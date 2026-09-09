# Speech

Provider-neutral speech-to-text and text-to-speech execution for configured task-model bindings.

## Overview

`core/model_tasks/` (`speech*.py`) executes file-based STT and TTS. It resolves the configured `speech_to_text` or `text_to_speech` binding through `TaskModelService`, merges stored options with backend schema defaults, parses the target, and routes to either a provider-backed speech HTTP client or an optional local speech executor hook. The server enforces `settings.json` `speech_upload_max_size_bytes` before calling `SpeechService.transcribe`; the default limit is 100 MiB (`104_857_600` bytes). Before either local or Provider-backed STT execution, `SpeechService` converts every accepted source recording to the live server-owned `speech.transcription_audio` profile so Chat microphone and post-Wakeword command audio reach the Model with the same container, mono PCM16 sample format, and sample rate.

This domain owns speech wire payloads and runtime artifacts; it does not own task-target discovery, settings validation, chat message persistence, or generic attachments. The first implementation supports OpenAI-compatible audio endpoints and OpenRouter's audio endpoints. Mistral option schemas may be exposed through the generic task-model layer, but Mistral speech execution currently fails through provider execution error handling until a provider runtime contract exists.

## Interfaces

- `SpeechService.transcribe(audio, filename, media_type) -> SpeechTranscriptionResult` - validates non-empty bytes, resolves the `speech_to_text` binding, converts the source through PyAV to the live transcription-audio profile, then calls the selected local executor or provider speech client with a canonical `recording.wav` / `audio/wav` or `recording.flac` / `audio/flac` payload. Besides the server transcribe endpoint, the chat layer's `ContentBlockResolver` uses this as its transcriber to degrade audio attachments to text (see `.vorch/domain-maps/attachments.md`).
- `SpeechService.synthesize(text) -> SpeechSynthesisResult` - trims and validates text, resolves the `text_to_speech` binding, then returns raw synthesized audio.
- `SpeechService.synthesize_artifact(text) -> SpeechArtifact` - calls `synthesize()` and persists one runtime artifact under the Runtime-injected canonical path `<data_dir>/artifacts/speech/`.
- `SpeechService.get_artifact(artifact_id) -> SpeechArtifact` - accepts bounded safe opaque IDs, reads the sidecar, recomputes `file_path`, and verifies the audio blob exists.
- `ProviderSpeechClient.transcribe(...)` / `ProviderSpeechClient.synthesize(...)` - small speech-specific HTTP clients built from runtime provider config, connection auth, credentials, and the target model ID.
- `LocalSpeechExecutor.transcribe(...)` dispatches to registered local engines; `synthesize(...)` dispatches to local TTS engines. `SpeechService.close/aclose` own local worker/model cleanup.

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

## Local engines

`speech_local.py` owns engine definitions, option schemas, dependency preflight,
audio chunking, adapters and lifecycle. Runtime passes one `LocalSpeechExecutor`
and its target registry to SpeechService and TaskModelService. Built-ins are
`local/qwen3-asr` (Qwen 1.7B or 0.6B, language/context options) and
`local/parakeet` (NVIDIA TDT v3). Both use native Transformers under the optional
`local-speech` extra. Configuration does not load weights; first non-silent use does.

To add an engine, supply a `SpeechEngineDefinition` with descriptor, factory and
optional load-affecting option names. Its synchronous `LocalTranscriptionEngine` or `LocalSynthesisEngine`
implements `transcribe` or `synthesize`, plus `close`; callers and accessors remain unchanged.
Unspecified load-option names mean every option participates in cache identity.
Each engine has its own cached model and bounded worker, serializing its loading,
inference and unloading outside the Event Loop. Other engines remain independent:
STT can be unloaded while TTS is busy. Changing load options replaces only that
engine's model; switching bindings or using a Provider does not evict other models.
Runtime shutdown closes all engines. Cancellation waits for already-started work
before reporting cancellation. Memory status is metadata-only; targeted manual
release refuses a busy engine immediately and retains downloaded files. Coverage:
`test_speech_local.py` checks independent residency, busy TTS during STT release,
cancellation, no-op release and loading again.

PyAV decodes canonical audio into mono float32 at 16 kHz. Chunks are at most 30
seconds, cut near a quiet point in the last second, with no discarded samples.
Exact digital silence skips inference. Result segment times are chunk bounds,
not word alignment. Models load from the Hugging Face cache/download or an
explicit server directory, with remote code disabled; offline mode permits
only cached files. The built-in loader first resolves a filtered Hugging Face
snapshot, then loads native Transformers classes exclusively from its local
path. Download callbacks report actual transfer activity; cached/local/offline
loads do not fabricate download progress.

`LocalSpeechSetup` in `speech_setup.py`, exposed through `SpeechService.local_setup`
and `local_setup_for(target)`, owns fixed-recipe installation jobs per Runtime.
The shared STT job and individual TTS jobs serialize package operations. It reads the shipped
`local-speech` extra, invokes the server interpreter's pip without reinstalling
vBot launchers, preserves compatible working Torch or installs an official
NVIDIA CUDA/CPU/platform build, and verifies imports plus NVIDIA execution in
a fresh process. Status is process-local and survives browser navigation;
duplicate requests share the job, failures permit explicit retry, shutdown
cancels and reaps the package subprocess. Raw package output stays private.
Local execution remains unavailable during installation, failure and the
verified restart-required state; a fresh Runtime rechecks package metadata.
See `USAGE.md` -> Local speech recognition for the user setup flow.

`SpeechProgress` is a request-local, thread-safe snapshot of phase and elapsed
time. `SpeechService.transcribe/synthesize(progress=...)` carries it to local workers
or Provider speech. Local factory signatures stay unchanged: the executor scopes
built-in loader reporting to its worker invocation and resets that context
afterwards. Queued, preparation, model checks/download/loading and inference
remain distinguishable; cached engines skip loading. No audio or transcript
is included in progress snapshots.

Coverage: `tests/core/model_tasks/test_speech_local.py` tests custom engine
substitution, cache/lifecycle, cancellation, decode/resampling/chunk coverage,
failures, request progress, fixed setup commands/retry/cancellation and native
adapter calls without downloading weights. `test_speech.py`
covers service error translation and Provider routing; Runtime registration and
cleanup are covered by `tests/core/runtime/test_runtime.py`.


Local TTS registrations are `local/qwen3-tts` (CustomVoice 1.7B/0.6B, preset
voices/languages, 1.7B style instructions) and `local/chatterbox` (Multilingual V3,
language, expressiveness/guidance). Their incompatible SDK dependencies are
installed into managed Python 3.12 environments under the Runtime-injected
`DataDirectoryLayout.speech_engines` root. `local-tts` installs only uv in the
server interpreter; shipped recipes install each SDK and matched Torch/audio
packages separately. Verification writes a recipe marker, never loads weights,
and makes TTS immediately available without restarting the server. A changed
recipe or missing interpreter requires setup again. Fixed upstream revisions
avoid accidentally selecting Chatterbox's older PyPI V2 implementation.

`speech_worker.py` starts without importing vBot, loads SDKs only inside its
child environment, reports actual download/load/generation phases and writes
mono PCM16 WAV to a parent-owned temporary path. The parent retains one process per TTS engine
while that engine's load options match, bounds requests to 5,000 characters / 64 MiB output,
and owns timeouts and whole-process-tree cleanup (Windows launchers have child
interpreters). Sentence/word chunking bounds each generation context. No voice
cloning input is exposed. Offline mode permits only cached model files. Native
Chatterbox watermarking remains enabled. Coverage: `test_speech_tts.py` covers
SDK calls, playable WAV chunks, process boundaries, setup isolation and availability.

## Provider Wire Behavior

Provider-backed speech execution does not call the chat provider adapters. `ProviderSpeechClient` subclasses `core.providers.task_client.ProviderTaskClient`, which owns the shared plumbing (constructor tuple, `from_runtime` target resolution, auth headers, POST/classify/parse cycle, retry policy - see `providers.md`); `core/model_tasks/speech_providers.py` owns only the speech payload shapes and response parsing.

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

All speech requests honor the universal `extra_options` escape hatch: for JSON payloads (OpenRouter STT, all TTS) the object adds non-empty fields at the top level (`merge_extra_options` from `core/providers/task_client.py`); for the multipart OpenAI-compatible STT path values are stringified as form fields (booleans as lowercase literals, containers JSON-encoded). Either path rejects a collision with an authored request field locally as a non-retryable Provider error before send.

Executable TTS targets send JSON to `/audio/speech` and return raw audio bytes. TTS is billed and non-idempotent: connect/connect-timeout/pool-timeout failures and HTTP 429 remain retryable, but ambiguous read/write/remote-protocol failures, every unverified 5xx response (including 502/503/504), and empty/unusable 2xx results stop after one attempt with `ProviderOutcomeUnknownError`. No current TTS endpoint profile sends an idempotency header. `voice` is taken from stored task-model options, populated from `model.capabilities.supported_voices` when the model provides them - OpenRouter models get model-specific voice lists (e.g. Kokoro 54, Gemini TTS 30, Voxtral 30); OpenAI models get the canonical OpenAI voice list; other providers fall back to free-text `voice` input. `response_format` per provider (OpenRouter `mp3`/`pcm`; OpenAI full set `mp3`/`opus`/`aac`/`flac`/`wav`/`pcm`). Numeric `speed` stays top-level for all providers. OpenRouter receives only OpenAI speaking instructions nested under `provider.options.openai.instructions` when `instructions` is set (gated on `model.capabilities.supported_parameters`); other OpenAI-compatible providers receive `instructions` at the top level. If the provider omits `content-type`, `SpeechSynthesisResult.media_type` is derived from `response_format`; an empty success body is rejected as an unknown outcome rather than persisted as an empty artifact.

## Server & Tool Contracts

- `POST /api/speech/transcribe` accepts multipart file upload, enforces the runtime upload limit before reading into `SpeechService`, and returns `SpeechTranscriptionResult.to_dict()`.
- With `Accept: application/x-ndjson`, the same upload streams request-local
  progress heartbeats and one terminal result/error. `server/app.py` owns this
  transport and cancels/reaps work on disconnect; local worker cancellation
  still waits for active inference. Ordinary JSON clients remain supported.
  Coverage: `tests/server/test_speech_endpoints.py`.
- `speech.local_setup_status/install/restart` in `server/rpc/settings_methods.py`
  accept an optional exact local `target`, never client commands, package names or paths. Restart requires verified
  setup and the server startup callback; its detached CLI lifecycle helper
  targets the exact running bind/data directory. Coverage: task-model RPC and
  server-main tests.
- `speech.local_memory_status` reports each engine's loaded/busy state.
  `speech.local_unload` requires one exact `local/...` target and returns whether
  release occurred plus refreshed memory status. It never unloads other engines
  or interrupts work. Specialized Models polls status and offers one button per
  model, disabling only the busy/unloaded model. RPC and component tests cover
  targeted release, invalid requests and independent controls.
- With `Accept: application/x-ndjson`, synthesis uses the same progress stream
  and returns a persisted speech artifact projection as its terminal result.
  The Settings preview uses its URL; ordinary clients still receive raw audio.
- `POST /api/speech/synthesize` accepts JSON `{ "text": "..." }`, rejects malformed JSON or blank text before calling `SpeechService`, and returns raw audio bytes with the synthesized media type.
- `GET /api/speech/artifacts/{artifact_id}` streams a persisted speech artifact through `FileResponse`.
- The built-in `text_to_speech` tool accepts only `text`; it returns a tool artifact payload from `SpeechArtifact.to_dict()` and intentionally exposes no model, provider, voice, format, or speed arguments.

## Artifacts

TTS tool output is stored under `<data_dir>/artifacts/speech/` through the shared `TaskArtifactStore` (`core/model_tasks/artifacts.py`): one audio file and one sidecar JSON metadata file per artifact. New artifact IDs use `aud_` plus 12 lowercase base32 characters, filenames are `<artifact_id>.<extension>`, and sidecars contain `id`, `filename`, `media_type`, and `size_bytes`. Speech artifacts are not normal attachments and are not persisted as chat messages by default.

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
- Local speech imports remain dependency-free; dependency availability does not promise GPU/model readiness. Device, checkpoint and memory errors are reported during execution as `SpeechExecutionError`; missing extras remain `SpeechUnsupportedTargetError`.
- Artifact persistence (shared `TaskArtifactStore`) exclusively reserves the sidecar name before writing the audio file and complete metadata; interrupted writes can leave invalid sidecars or orphaned audio blobs, whose names remain occupied.
- No credentials may be logged, persisted in artifacts, or returned to accessors.
