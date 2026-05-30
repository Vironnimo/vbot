# Speech

Provider-neutral speech-to-text and text-to-speech execution, backed by the
central task-model bindings.

## Overview

`core/speech/` executes file-based STT and TTS. It resolves the configured
`speech_to_text` or `text_to_speech` binding through `TaskModelService`, merges
stored options with backend schema defaults, parses the target, and then routes
to either a provider-backed HTTP client or a local speech executor hook.

The first implementation supports OpenAI-compatible audio endpoints and
OpenRouter's audio endpoints. Mistral option schemas may be exposed through the
generic task-model layer, but Mistral speech execution currently returns an
expected unsupported-provider error until the provider runtime contract is
implemented.

## Interfaces

- `SpeechService.transcribe(audio, filename, media_type) ->
  SpeechTranscriptionResult`
- `SpeechService.synthesize(text) -> SpeechSynthesisResult`
- `SpeechService.synthesize_artifact(text) -> SpeechArtifact`
- `SpeechService.get_artifact(artifact_id) -> SpeechArtifact`
- `ProviderSpeechClient.transcribe(...)`
- `ProviderSpeechClient.synthesize(...)`
- `LocalSpeechExecutor.transcribe(...)`
- `LocalSpeechExecutor.synthesize(...)`

`SpeechTranscriptionResult` contains normalized `text`, optional `language`,
optional `segments`, optional `usage`, and the raw response payload when
available.

`SpeechSynthesisResult` contains raw audio bytes, media type, response format,
and optional generation id.

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

OpenRouter STT sends JSON to `/audio/transcriptions`:

```json
{
  "model": "openai/gpt-4o-transcribe",
  "input_audio": {
    "data": "<base64-audio>",
    "format": "webm"
  }
}
```

`language: "auto"` is omitted from the provider request. Numeric
`temperature` is forwarded. Provider-specific `provider` options are preserved
for OpenRouter when present.

OpenAI-compatible STT sends multipart form data to `/audio/transcriptions` with
`file`, `model`, and normalized optional fields such as `language`, `prompt`,
`response_format`, and `temperature`.

TTS sends JSON to `/audio/speech` and returns raw audio bytes. OpenRouter
receives OpenAI speaking instructions nested under
`provider.options.openai.instructions`; other OpenAI-compatible providers
receive `instructions` at the top level.

## Artifacts

TTS tool output is stored under `<data_dir>/speech/` as one audio file and one
sidecar JSON metadata file per artifact. Artifact IDs are hex UUID strings and
are served by `GET /api/speech/artifacts/{artifact_id}`. Speech artifacts are
not normal attachments and are not persisted as chat messages by default.

## Errors

Expected speech errors inherit from `SpeechError`:

- `SpeechConfigurationError` for missing bindings, empty input, invalid artifact
  ids, and missing artifacts.
- `SpeechUnsupportedTargetError` for configured local or provider targets with
  no execution adapter.
- `SpeechExecutionError` for provider/network/runtime request failures.

Provider request failures are logged through `vbot.speech` without credentials.
Transient network and HTTP errors use the shared provider retry helpers.

## Constraints & Gotchas

- Speech uses file-based requests only. Realtime voice sessions and partial STT
  streaming are out of scope for this domain version.
- Binary audio transport stays outside JSON-RPC. Accessors use dedicated HTTP
  endpoints for recording upload and synthesized audio download.
- Do not expose model, provider, voice, or format arguments through the agent
  TTS tool. Those choices are central settings controlled by the user.
- Local speech execution hooks must stay optional and dependency-free until a
  concrete local backend is approved.
- No credentials may be logged, persisted in artifacts, or returned to accessors.
