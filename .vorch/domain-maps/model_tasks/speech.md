# Speech

Provider-neutral speech-to-text and text-to-speech execution for configured task-model bindings.

## Overview

`core/model_tasks/` (`speech*.py`) executes file-based STT and TTS. It resolves the configured `speech_to_text` or `text_to_speech` binding through `TaskModelService`, merges stored options with backend schema defaults, parses the target, and routes to either a provider-backed speech HTTP client or an optional local speech executor hook. The server enforces `settings.json` `speech_upload_max_size_bytes` before calling `SpeechService.transcribe`; the default limit is 100 MiB (`104_857_600` bytes). Before either local or Provider-backed STT execution, `SpeechService` converts every accepted source recording to the live server-owned `speech.transcription_audio` profile so Chat microphone and post-Wakeword command audio reach the Model with the same container, mono PCM16 sample format, and sample rate.

This domain owns speech wire payloads and runtime artifacts; it does not own task-target discovery, settings validation, chat message persistence, or generic attachments. The first implementation supports OpenAI-compatible audio endpoints and OpenRouter's audio endpoints. Mistral option schemas may be exposed through the generic task-model layer, but Mistral speech execution currently fails through provider execution error handling until a provider runtime contract exists.

## Interfaces

- `SpeechService.transcribe(audio, filename, media_type) -> SpeechTranscriptionResult` — validates non-empty bytes, resolves the `speech_to_text` binding, converts the source through PyAV to the live transcription-audio profile, then calls the selected local executor or provider speech client with a canonical `recording.wav` / `audio/wav` or `recording.flac` / `audio/flac` payload. Besides the server transcribe endpoint, the chat layer's `ContentBlockResolver` uses this as its transcriber to degrade audio attachments to text (see `.vorch/domain-maps/attachments.md`).
- `SpeechService.synthesize(text) -> SpeechSynthesisResult` — trims and validates text, resolves the `text_to_speech` binding, then returns raw synthesized audio.
- `SpeechService.synthesize_artifact(text) -> SpeechArtifact` — calls `synthesize()` and persists one runtime artifact under the Runtime-injected canonical path `<data_dir>/artifacts/speech/`.
- `SpeechService.get_artifact(artifact_id) -> SpeechArtifact` — accepts only 32-character lowercase hex IDs, reads the sidecar, recomputes `file_path`, and verifies the audio blob exists.
- `ProviderSpeechClient.transcribe(...)` / `ProviderSpeechClient.synthesize(...)` — small speech-specific HTTP clients built from runtime provider config, connection auth, credentials, and the target model ID.
- `LocalSpeechExecutor.transcribe(...)` / `LocalSpeechExecutor.synthesize(...)` — optional extension hooks; the default executor raises `LocalSpeechError` for every target.

`SpeechTranscriptionResult` contains normalized `text`, optional `language`, optional `segments`, optional `usage`, and the raw response payload when available.

`SpeechSynthesisResult` contains raw audio bytes, media type, response format, and optional generation id.

`SpeechArtifact.to_dict()` returns:

```json
{
  "id": "f1e2d3c4...",
  "kind": "speech",
  "filename": "f1e2d3c4....mp3",
  "media_type": "audio/mpeg",
  "size_bytes": 1234,
  "url": "/api/speech/artifacts/f1e2d3c4..."
}
```

## Provider Wire Behavior

Provider-backed speech execution does not call the chat provider adapters. `ProviderSpeechClient` subclasses `core.providers.task_client.ProviderTaskClient`, which owns the shared plumbing (constructor tuple, `from_runtime` target resolution, auth headers, POST/classify/parse cycle, retry policy — see `providers.md`); `core/model_tasks/speech_providers.py` owns only the speech payload shapes and response parsing.

OpenRouter STT sends Base64 JSON to `/audio/transcriptions`; the default compatibility profile produces:

```json
{
  "model": "openai/gpt-4o-transcribe",
  "input_audio": {
    "data": "<base64-audio>",
    "format": "wav"
  }
}
```

`language: "auto"` is omitted from the provider request. Numeric `temperature` is forwarded. Provider-specific `provider` options are preserved for OpenRouter when present.

Executable non-OpenRouter STT targets are treated as OpenAI-compatible audio endpoints and send multipart form data to `/audio/transcriptions` with `file`, `model`, and normalized optional fields such as `language`, `prompt`, `response_format`, and `temperature`.

All speech requests honor the universal `extra_options` escape hatch: for JSON payloads (OpenRouter STT, all TTS) the object merges into the top-level payload last and overrides authored keys (`merge_extra_options` from `core/providers/task_client.py`); for the multipart OpenAI-compatible STT path values are stringified as form fields (booleans as lowercase literals, containers JSON-encoded).

Executable TTS targets send JSON to `/audio/speech` and return raw audio bytes. TTS is billed and non-idempotent: connect/connect-timeout/pool-timeout failures and HTTP 429 remain retryable, but ambiguous read/write/remote-protocol failures, every unverified 5xx response (including 502/503/504), and empty/unusable 2xx results stop after one attempt with `ProviderOutcomeUnknownError`. No current TTS endpoint profile sends an idempotency header. `voice` is taken from stored task-model options, populated from `model.capabilities.supported_voices` when the model provides them — OpenRouter models get model-specific voice lists (e.g. Kokoro 54, Gemini TTS 30, Voxtral 30); OpenAI models get the canonical OpenAI voice list; other providers fall back to free-text `voice` input. `response_format` per provider (OpenRouter `mp3`/`pcm`; OpenAI full set `mp3`/`opus`/`aac`/`flac`/`wav`/`pcm`). Numeric `speed` stays top-level for all providers. OpenRouter receives only OpenAI speaking instructions nested under `provider.options.openai.instructions` when `instructions` is set (gated on `model.capabilities.supported_parameters`); other OpenAI-compatible providers receive `instructions` at the top level. If the provider omits `content-type`, `SpeechSynthesisResult.media_type` is derived from `response_format`; an empty success body is rejected as an unknown outcome rather than persisted as an empty artifact.

## Server & Tool Contracts

- `POST /api/speech/transcribe` accepts multipart file upload, enforces the runtime upload limit before reading into `SpeechService`, and returns `SpeechTranscriptionResult.to_dict()`.
- `POST /api/speech/synthesize` accepts JSON `{ "text": "..." }`, rejects malformed JSON or blank text before calling `SpeechService`, and returns raw audio bytes with the synthesized media type.
- `GET /api/speech/artifacts/{artifact_id}` streams a persisted speech artifact through `FileResponse`.
- The built-in `text_to_speech` tool accepts only `text`; it returns a tool artifact payload from `SpeechArtifact.to_dict()` and intentionally exposes no model, provider, voice, format, or speed arguments.

## Artifacts

TTS tool output is stored under `<data_dir>/artifacts/speech/` through the shared `TaskArtifactStore` (`core/model_tasks/artifacts.py`): one audio file and one sidecar JSON metadata file per artifact. Artifact IDs are UUID4 hex strings, filenames are `<artifact_id>.<extension>`, and sidecars contain `id`, `filename`, `media_type`, and `size_bytes`. Speech artifacts are not normal attachments and are not persisted as chat messages by default.

## Errors

Callers of `SpeechService` should see expected speech errors as `SpeechError` subclasses (`SpeechError` derives from the shared `TaskError` base in `core/utils/errors.py`):

- `SpeechConfigurationError` for missing bindings, empty input, invalid artifact ids, and missing artifacts.
- `SpeechUnsupportedTargetError` for configured local targets with no execution adapter.
- `SpeechExecutionError` for provider/network/runtime request failures.
- `SpeechOutcomeUnknownError` for TTS requests that may have completed but cannot be safely replayed; the `text_to_speech` Tool returns `provider_outcome_unknown`, `retryable: false`, plus the operation key in its message.

Missing STT bindings and Provider request failures are logged through `vbot.speech` without credentials; Provider/network failures raised inside `ProviderSpeechClient` and source-audio decode/convert failures are wrapped as `SpeechExecutionError`, with the TTS unknown-outcome subtype preserved for Tool/UI/log correlation. The server maps `SpeechConfigurationError` to HTTP 409, `SpeechUnsupportedTargetError` to 422, and `SpeechExecutionError` to 502. STT retains the shared historical provider retry policy; only TTS opts into the stricter non-idempotent policy.

## Constraints & Gotchas

- Speech uses file-based requests only. Realtime voice sessions and partial STT streaming are out of scope for this domain version.
- Transcription conversion depends on PyAV from the `[server]` dependency group; its binary wheels carry FFmpeg support for decoding browser WebM/Opus input and encoding the supported WAV/PCM16 and FLAC/PCM16 output profiles. Conversion runs in a worker thread so media decoding does not block the server event loop.
- The built-in profiles are `compatibility` (WAV, mono PCM16, 16 kHz) and `high_quality` (FLAC, mono PCM16, 48 kHz); `custom` accepts WAV or FLAC at 16, 24, or 48 kHz. The server setting is live-read for every transcription, while the upload-size limit remains restart-applied.
- Binary audio transport stays outside JSON-RPC. Accessors use dedicated HTTP endpoints for recording upload and synthesized audio download.
- The speech HTTP client is not the chat adapter stack. Provider-specific chat behavior, debug capture, streaming behavior, or message formatting changes do not automatically apply here.
- Local speech execution hooks must stay optional and dependency-free until a concrete local backend is approved.
- Artifact persistence (shared `TaskArtifactStore`) writes the audio file before the JSON sidecar and currently has no rollback/atomic replace wrapper; interrupted writes can leave orphaned audio blobs.
- No credentials may be logged, persisted in artifacts, or returned to accessors.
