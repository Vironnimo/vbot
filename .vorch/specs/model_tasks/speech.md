# Speech

Provider-neutral speech-to-text and text-to-speech execution for configured task-model bindings.

## Overview

`core/model_tasks/` (`speech*.py`) executes file-based STT and TTS. It resolves the configured `speech_to_text` or `text_to_speech` binding through `TaskModelService`, merges stored options with backend schema defaults, parses the target, and routes to either a provider-backed speech HTTP client or an optional local speech executor hook. The server enforces `settings.json` `speech_upload_max_size_bytes` before calling `SpeechService.transcribe`; the default limit is 20 MiB (`20_971_520` bytes).

This domain owns speech wire payloads and runtime artifacts; it does not own task-target discovery, settings validation, chat message persistence, or generic attachments. The first implementation supports OpenAI-compatible audio endpoints and OpenRouter's audio endpoints. Mistral option schemas may be exposed through the generic task-model layer, but Mistral speech execution currently fails through provider execution error handling until a provider runtime contract exists.

## Interfaces

- `SpeechService.transcribe(audio, filename, media_type) -> SpeechTranscriptionResult` — validates non-empty bytes, resolves the `speech_to_text` binding, then calls the selected local executor or provider speech client. Besides the server transcribe endpoint, the chat layer's `ContentBlockResolver` uses this as its transcriber to degrade audio attachments to text (see `.vorch/specs/attachments.md`).
- `SpeechService.synthesize(text) -> SpeechSynthesisResult` — trims and validates text, resolves the `text_to_speech` binding, then returns raw synthesized audio.
- `SpeechService.synthesize_artifact(text) -> SpeechArtifact` — calls `synthesize()` and persists one runtime artifact under `<data_dir>/speech/`.
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

OpenRouter STT sends Base64 JSON to `/audio/transcriptions`:

```json
{
  "model": "openai/gpt-4o-transcribe",
  "input_audio": {
    "data": "<base64-audio>",
    "format": "webm"
  }
}
```

`language: "auto"` is omitted from the provider request. Numeric `temperature` is forwarded. Provider-specific `provider` options are preserved for OpenRouter when present.

Executable non-OpenRouter STT targets are treated as OpenAI-compatible audio endpoints and send multipart form data to `/audio/transcriptions` with `file`, `model`, and normalized optional fields such as `language`, `prompt`, `response_format`, and `temperature`.

Executable TTS targets send JSON to `/audio/speech` and return raw audio bytes. `voice` is taken from stored task-model options, populated from `model.capabilities.supported_voices` when the model provides them — OpenRouter models get model-specific voice lists (e.g. Kokoro 54, Gemini TTS 30, Voxtral 30); OpenAI models get the canonical OpenAI voice list; other providers fall back to free-text `voice` input. `response_format` per provider (OpenRouter `mp3`/`pcm`; OpenAI full set `mp3`/`opus`/`aac`/`flac`/`wav`/`pcm`). Numeric `speed` stays top-level for all providers. OpenRouter receives only OpenAI speaking instructions nested under `provider.options.openai.instructions` when `instructions` is set (gated on `model.capabilities.supported_parameters`); other OpenAI-compatible providers receive `instructions` at the top level. If the provider omits `content-type`, `SpeechSynthesisResult.media_type` is derived from `response_format`.

## Server & Tool Contracts

- `POST /api/speech/transcribe` accepts multipart file upload, enforces the runtime upload limit before reading into `SpeechService`, and returns `SpeechTranscriptionResult.to_dict()`.
- `POST /api/speech/synthesize` accepts JSON `{ "text": "..." }`, rejects malformed JSON or blank text before calling `SpeechService`, and returns raw audio bytes with the synthesized media type.
- `GET /api/speech/artifacts/{artifact_id}` streams a persisted speech artifact through `FileResponse`.
- The built-in `text_to_speech` tool accepts only `text`; it returns a tool artifact payload from `SpeechArtifact.to_dict()` and intentionally exposes no model, provider, voice, format, or speed arguments.

## Artifacts

TTS tool output is stored under `<data_dir>/speech/` as one audio file and one sidecar JSON metadata file per artifact. Artifact IDs are UUID4 hex strings, filenames are `<artifact_id>.<extension>`, and sidecars contain `id`, `filename`, `media_type`, and `size_bytes`. Speech artifacts are not normal attachments and are not persisted as chat messages by default.

## Errors

Callers of `SpeechService` should see expected speech errors as `SpeechError` subclasses (`SpeechError` derives from the shared `TaskError` base in `core/utils/errors.py`):

- `SpeechConfigurationError` for missing bindings, empty input, invalid artifact ids, and missing artifacts.
- `SpeechUnsupportedTargetError` for configured local targets with no execution adapter.
- `SpeechExecutionError` for provider/network/runtime request failures.

Provider request failures raised inside `ProviderSpeechClient` are `ProviderError`/network errors; `SpeechService` wraps them as `SpeechExecutionError` and logs through `vbot.speech` without credentials. The server maps `SpeechConfigurationError` to HTTP 409, `SpeechUnsupportedTargetError` to 422, and `SpeechExecutionError` to 502. Transient network and retryable HTTP errors use the shared provider retry helper.

## Constraints & Gotchas

- Speech uses file-based requests only. Realtime voice sessions and partial STT streaming are out of scope for this domain version.
- Binary audio transport stays outside JSON-RPC. Accessors use dedicated HTTP endpoints for recording upload and synthesized audio download.
- The speech HTTP client is not the chat adapter stack. Provider-specific chat behavior, debug capture, streaming behavior, or message formatting changes do not automatically apply here.
- Local speech execution hooks must stay optional and dependency-free until a concrete local backend is approved.
- Artifact persistence writes the audio file before the JSON sidecar and currently has no rollback/atomic replace wrapper; interrupted writes can leave orphaned audio blobs.
- No credentials may be logged, persisted in artifacts, or returned to accessors.
