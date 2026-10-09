# Speech

Provider-neutral speech-to-text and text-to-speech execution for configured task-model bindings.

## Overview

`core/model_tasks/` (`speech*.py`) executes file-based STT and TTS. It resolves the configured `speech_to_text` or `text_to_speech` binding through `TaskModelService`, merges stored options with backend schema defaults, parses the target, and routes to either a provider-backed speech HTTP client or an optional local speech executor hook. STT preparation follows the target: local engines decode the original recording directly, while Provider requests preserve a supported original after validation and use `speech.transcription_audio` only when conversion is needed. The service owns input limits for every caller; the HTTP edge also enforces the per-upload limit (Transcription input below).

This domain owns speech wire payloads and runtime artifacts; it does not own task-target discovery, settings validation, chat message persistence, or generic attachments. The first implementation supports OpenAI-compatible audio endpoints and OpenRouter's audio endpoints. Mistral option schemas may be exposed through the generic task-model layer, but Mistral speech execution currently fails through provider execution error handling until a provider runtime contract exists.

## Interfaces

- `SpeechService.transcribe(audio, filename, media_type) -> SpeechTranscriptionResult` - validates non-empty bytes and the configured input-size limit, resolves the `speech_to_text` binding, and prepares audio for that target (Transcription input below). Besides the server transcribe endpoint, the chat layer's `ContentBlockResolver` uses this as its transcriber to degrade audio attachments to text (see `.vorch/domain-maps/attachments.md`).
- `SpeechService.synthesize(text) -> SpeechSynthesisResult` - trims and validates text, resolves the `text_to_speech` binding, then returns raw synthesized audio.
- `SpeechService.synthesize_artifact(text) -> SpeechArtifact` - calls `synthesize()` and persists one runtime artifact under the Runtime-injected canonical path `<data_dir>/artifacts/speech/`.
- Both synthesis methods accept an optional async `on_audio(SpeechAudioChunk)` callback. Chunks contain mono PCM16 little-endian `audio` bytes and `sample_rate_hz`; they arrive before the complete result and do not replace the final result/artifact. Playback consumers own presentation and cancellation.
- `SpeechService.get_artifact(artifact_id) -> SpeechArtifact` - accepts bounded safe opaque IDs, reads the sidecar, recomputes `file_path`, and verifies the audio blob exists.
- `ProviderSpeechClient.transcribe(...)` / `ProviderSpeechClient.synthesize(...)` - small speech-specific HTTP clients built from runtime provider config, connection auth, credentials, and the target model ID.
- `LocalSpeechExecutor.transcribe(...)` dispatches to registered local engines; `synthesize(...)` dispatches to local TTS engines. `SpeechService.close/aclose` own playback and local worker/model cleanup.

`SpeechService.playbacks` owns transient playback resources through the internal
`speech_playback.py` store. Each synthesis may create a resource and append its
PCM callback output; consumers receive a URL, never audio in Run events/history.
The dedicated HTTP stream frames each PCM block with little-endian uint32 sample
rate and byte count; a zero rate marks the terminal frame (empty success, otherwise
a stable error code). Each resource spools the complete playback to a temporary
file; absent or slow listeners never pace synthesis or lose early audio. File I/O
uses a separate bounded worker pool; readers receive frames of at most 32 KiB PCM.
The store keeps at most 16 resources. Successful resources accept new readers for
60 seconds after completion; existing readers may finish after that lookup expires.
Failure/cancellation ends playback immediately. Expiry removes idle temporary
files, and shutdown cancels playback and joins file cleanup through `aclose`.

`SpeechTranscriptionResult` contains normalized `text`, optional `language`, optional `segments`, optional `usage`, and the raw response payload when available.

`SpeechSynthesisResult` contains raw audio bytes, media type, response format, and optional generation id.

Speech execution writes durable Model Usage through the Runtime-injected recorder before returning or writing an artifact. Provider requests record each actual POST attempt; local calls record their execution boundary. Existing STT telemetry is retained, while absent counters (including ordinary binary TTS responses) remain unknown rather than measured zero. TTS Tool calls pass internal `TaskUsageContext` for the initiating Run and Extension scope; standalone upload/preview calls have no invented Session. Artifact deletion or write failure does not erase consumption. Coverage: `tests/core/model_tasks/test_task_usage.py`, `tests/core/tools/test_speech_tool.py`.

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

## Transcription input

`speech_input.py` owns bounded PyAV decoding and Provider input preparation. File
names and supplied media types do not establish the format: container and codec
must agree with the supported source shape. A Provider-supported original within
the target's byte budget is fully decoded for validation, then sent byte-for-byte
unchanged with a canonical `recording.<format>` name and media type. Unsupported
sources, mixed audio/video containers, and originals over the target's byte budget
use the configured WAV/FLAC fallback in one decode-and-encode pass. No HTTP request
is sent if validation or conversion fails.

Local STT bypasses this Provider conversion and does not read the fallback profile.
Its existing engine worker decodes the original once to mono float32 at 16 kHz,
feeding bounded inference chunks as samples arrive. Both paths validate actual
decoded samples, not a container's claimed duration: at most 30 minutes, sample
rate at most 192 kHz, at most eight channels, and at most 16 MiB in any decoded
frame. Malformed input remains an input error; it does not invalidate an otherwise
healthy cached local Model.

`speech_upload_max_size_bytes` (public path `speech.upload_max_size_bytes`, default
100 MiB / `104_857_600` bytes) is restart-applied and injected into `SpeechService`.
It therefore also bounds internal transcription callers, not just multipart
uploads. Provider preparation and sending additionally enforce vBot's conservative
25,000,000-byte audio-file ceiling; the encoder refuses output beyond that ceiling.
OpenRouter also has a vBot-owned 36,000,000-byte serialized JSON request ceiling,
counting UTF-8 options and the exact Base64 expansion before allocating the Base64
string. These are vBot policy limits, not a claim that every routed upstream has
the same maximum (Provider Wire Behavior below).

`SpeechService` keeps at most four pending STT requests: two executing (including
preparation) and two waiting. A further request fails with `SpeechBusyError` before
processing; a cancelled waiter releases its place. This bounds the service queue,
not HTTP bodies already received before the call. Provider decode/convert jobs use a separate two-worker
`BoundedWorkerPool`; cancellation signals frame-boundary checks and joins worker
cleanup before releasing admission. Local decoding uses the engine's existing
worker and cancellation path. Coverage: `test_speech.py`, `test_speech_input.py`,
`test_speech_local.py`, and `test_speech_providers.py`.

## Local engines

`speech_local.py` owns engine definitions, option schemas, dependency preflight,
audio chunking, adapters and lifecycle; `speech_models.py` (`SPEECH_MODELS`) pins
every built-in target's Model: repository, commit revision, and each file's SHA-256
and size. Moving a target to a newer upstream revision means editing its entry there.
Runtime passes one `LocalSpeechExecutor`
and its target registry to SpeechService and TaskModelService. Built-in STT targets are
`local/qwen3-asr-1.7b` and `local/qwen3-asr-0.6b` (language/context options),
`local/parakeet` (NVIDIA TDT v3), and `local/nemotron3.5-asr` (NVIDIA Nemotron 3.5
ASR Streaming 0.6B, automatic or explicit language). The Qwen and Nemotron `language`
option is a select: `""` (Automatic) plus the codes that engine's processor accepts,
labelled with English names from `_LANGUAGE_NAMES` (Qwen: the codes of its forced-language
prompt; Nemotron: prompt dictionary keys of the locales it emits, a bare code where the
dictionary maps one, the other regional variant by its locale, e.g. `en` (US) and `en-GB`). One target is one Model; there is
no `model` option. Descriptor `metadata` is `{license, download_bytes}` for install UIs.
All use native Transformers under the optional
`local-speech` extra. Configuration does not load weights; first non-silent use does,
unless a preload or preparation request (below) started the load earlier.

To add an engine, supply a `SpeechEngineDefinition` with descriptor, factory,
optional load-affecting option names, and its pinned `model` (plus `environment`, the
TTS environment it runs in; empty means the STT stack). Its synchronous `LocalTranscriptionEngine` or `LocalSynthesisEngine`
implements `transcribe` or `synthesize`, plus `close`; callers and accessors remain unchanged.
Unspecified load-option names mean every option participates in cache identity.
Each engine has its own cached model and bounded worker, serializing its loading,
inference and unloading outside the Event Loop. Other engines remain independent:
STT can be unloaded while TTS is busy. Changing load options replaces only that
engine's model; switching bindings or using a Provider does not evict other models.
Runtime shutdown closes all engines. A cancelled STT or TTS request that is still
queued never starts (`LocalSpeechExecutor._run`); a running one starts no further
engine call (STT: no further chunk), and its caller waits until the running call has
finished, so the engine is never reported idle while it works. A worker child (managed
STT, every TTS engine) that has not answered that call within `_CANCEL_GRACE_S` (30 s)
is ended (`abort()`, optional on both engine protocols) and its model unloaded; an
in-process engine cannot be interrupted and always finishes.
The same cancellation grace covers a request-owned managed cold load; after any
cancelled load, no inference starts. Cancelling a request queued behind a shared
background preload leaves that preload running. Memory status is metadata-only;
targeted manual release refuses a busy engine immediately and keeps the installed Model files. Coverage:
`test_speech_local.py` checks independent residency, busy TTS during STT release,
cancellation, no-op release and loading again.

Preloading avoids the cold load on the first transcription or synthesis (tens of
seconds for a packaged engine). Every local STT and TTS engine has the boolean option
`preload` ("Load at server start", `PRELOAD_OPTION`, default off). It is not a load
option, so toggling it never reloads and turning it off unloads nothing.
`SpeechService.preload_configured(task_types)` starts a background load of each bound
engine whose option is on; the server lifespan calls it for both bindings after startup
(not in a safe startup mode) and `Runtime.apply_settings_change` for each of
`model_tasks.speech_to_text` and `text_to_speech` whose binding changed.
`SpeechService.prepare_transcription()`
starts the same load regardless of the option and returns at once with `loaded`,
`loading`, `not_local` (Provider binding) or `unavailable`. Both use
`LocalSpeechExecutor.prepare(local_id, options)` (Event Loop only, STT or TTS): the load
runs on the engine's worker, so a request arriving meanwhile queues behind it and
reuses the model; a pending load with the same load identity is not started
twice; failures are logged and left for the next transcription to report.
Shutdown cancels a preload that has not started and kills every worker child the
server is waiting on, loading, transcribing or synthesizing (`_WaitingWorkers`); its
request fails as closed. An in-process load or inference (development checkout) cannot be interrupted
and delays shutdown until it finishes. The real load, including a managed
child's, logs one INFO line with the engine and load seconds after it completes
(its start only at DEBUG). Coverage: `test_speech_local.py` (prepare states, dedupe, waiting
transcription, failed preload, shutdown during a managed preload, every engine offers
`preload`), `test_speech_tts.py` (preload reused by synthesis), `test_speech.py`,
`test_runtime_settings.py`, `tests/server/test_app.py`.

PyAV decodes the original recording once into mono float32 at 16 kHz. Chunks are at most 30
seconds, cut near a quiet point in the last second, with no discarded samples.
Decoded frames fill a fixed-size chunk buffer; only the unconsumed tail moves to
the next buffer, so small frames do not repeatedly copy the accumulated recording.
Yielded sample arrays stay unchanged while later chunks are assembled.
Exact digital silence skips inference. Result segment times are chunk bounds,
not word alignment. Engines never download: they load only from a local directory,
with remote code disabled and `local_files_only`. The executor passes the target's
installed Model directory as the `model_path` option unless the binding sets its own
("Model directory": a server directory with a Transformers model of that engine's
architecture, loaded instead of the installed Model; a missing directory fails with a
configuration message; the field is a `server_path` directory, so Settings browses the
server's folders for it). There is no offline option in the schema; a ready target
always runs offline. Coverage: `test_speech_local.py` checks the injected `model_path`
and all three STT adapter contracts, `test_speech_tts.py` the TTS worker's offline load.

Nemotron uses native `AutoModelForRNNT` with language prompt ids. Within each
recording segment, one feature extraction feeds fixed-size mel chunks into
cache-aware generation (560 ms context); short tails are padded, never dropped.
The output bound accounts for RNNT blank emissions. Generated locale tags are
removed from text and exposed as language metadata when present; generation
caches do not cross recordings. `test_speech_local.py` covers frame boundaries,
prompt integer dtype, language projection and separate memory/setup identity.
This internal streaming does not introduce partial-text transport or realtime
microphone Sessions.

`LocalSpeechSetup` in `speech_setup.py` is one installation job per target and Runtime
(`LocalSpeechExecutor.setups`, reached through `SpeechService.local_setup_for(target)`):
the target's engine environment, then its pinned Model.
It extends `LocalSetup` (`local_setup.py`), which local embeddings share: status,
job lifecycle, receipts, the Model fetch (`model_tasks.md` -> Overview) and the uv
recipe installer (`_install_recipe`) live there;
the server-interpreter STT path and the TTS verification stay in `speech_setup.py`.
Targets of one engine share its environment under `DataDirectoryLayout.speech_engines`
(`stt/` for managed STT, `qwen3-tts/` for both Qwen3-TTS sizes, `chatterbox/`), while each
target's Model lives in `models/<local id>/<revision>/` there. Install sets up the
environment only when it is missing or unusable, then fetches the Model unless its
receipt exists; `ready` means the target runs offline. All target jobs share one
install lock (`queued` while another runs). In a packaged release,
STT and TTS use managed data-directory environments and child processes; the immutable release
runtime is not changed. Managed verification and execution workers use `-I -B`:
isolated mode alone ignores `PYTHONDONTWRITEBYTECODE` and would allow STT imports
to create bytecode inside the release. Managed STT installs both the declared core dependencies and the
local-speech extra because its worker imports vBot source. Its startup imports only
the local speech implementation through the lazy Task Model package facade, so
unrelated task dependencies do not become STT requirements. The development-checkout STT path
retains its legacy server-interpreter pip recipe; its STT targets share one
`ServerSpeechStack`, so once that install changed the server's packages every STT
target reports `restart_required` (and install returns that status) until the server
restarts. Fixed recipes preserve compatible Torch
or install the selected CUDA/CPU build and verify
imports plus NVIDIA execution in a fresh process. Status is process-local and survives browser
navigation; duplicate requests share the job, failures permit explicit retry, shutdown cancels and
reaps the package subprocess. Raw package output stays private. Local execution remains unavailable
during installation, failure and the verified restart-required state. Development-checkout STT
rechecks package metadata; managed STT/TTS require the environment interpreter, the base
Python its `pyvenv.cfg` names (`python_missing` once that is gone, e.g. after the application
was installed again) with the `major.minor` the environment requires (`python_changed`
otherwise; `model_tasks.md` -> Overview): for STT the server's (it has no recipe: its worker
imports vBot source), for TTS its recipe's `python`, else also the server's. They also
require a
`verified.json` completion receipt, written atomically only after successful verification
and removed before package changes. Every target also needs its Model receipt
(`model_missing` otherwise). Setup removes and recreates an environment that fails one of
these Python checks. A check that cannot read the interpreter, base Python, `pyvenv.cfg` or a
receipt reports `environment_unreadable` instead (`local_setup.environment_error`), and setup
never removes such an environment. Environment receipt contents are not runtime compatibility data:
changed dependency declarations or worker source never invalidate a completed setup;
only a changed required Python does (above). A changed pinned revision does too: the target is `model_missing` until installed again.
Actual SDK, device and Model failures are handled at execution. Status reads neither
import ML runtimes nor start subprocesses, rewrite receipts or run setup. Explicit
`task_model.status` checks report the selected local speech environment's concrete
unavailability reason once per change, plus recovery; catalog enumeration remains silent.
See `USAGE.md` -> Local speech recognition for the user setup flow.

The managed STT child (`speech_worker.py --stt <engine> <app_root>`) serves one
engine over JSON lines: `{"load": true, "options"}` loads the model and answers
`{"loaded": true}`; a samples request answers `{"result"}`; `{"phase"}` frames
forward loader and inference progress; `{"error"}` ends the child.
`_ManagedSttEngine` construction returns only after `loaded`, so the executor's
load boundary (logs, progress, preloading) covers the child's real load.
Every request has a deadline (`_LOAD_DEADLINE_S` 900 s for the load,
`_INFERENCE_DEADLINE_S` 600 s per chunk of at most 30 s audio): a child that has not
answered by then is ended with its process tree, the model unloaded, and the request
fails with `LocalSpeechExecutionError`; the next request loads again.
`test_speech_local.py` runs this protocol against a real child with a fake engine,
including a child that stops answering (deadline, cancellation, shutdown).

`SpeechProgress` is a request-local, thread-safe snapshot of phase and elapsed
time. `SpeechService.transcribe/synthesize(progress=...)` carries it to local workers
or Provider speech. Local factory signatures stay unchanged: the executor scopes
built-in loader reporting to its worker invocation and resets that context
afterwards. Phases are `queued`, `preparing`, `loading` and `transcribing` /
`synthesizing`; cached engines skip loading. Downloads happen only during installation. No audio or transcript
is included in progress snapshots.

Coverage: `tests/core/model_tasks/test_speech_local.py` tests custom engine
substitution, cache/lifecycle, cancellation, decode/resampling/chunk coverage,
failures, isolated managed-worker startup, request progress, fixed setup commands/retry/cancellation and native
adapter calls without downloading weights. `test_speech.py`
covers service error translation and Provider routing; Runtime registration and
cleanup are covered by `tests/core/runtime/test_runtime_lifecycle.py`.


Local TTS registrations are `local/qwen3-tts-1.7b` and `local/qwen3-tts-0.6b`
(CustomVoice, preset voices/languages; style instructions only on 1.7B) and
`local/chatterbox` (Multilingual V3, language, expressiveness/guidance). Their
incompatible SDK dependencies are
installed into managed environments under the Runtime-injected
`DataDirectoryLayout.speech_engines` root; both Qwen3-TTS sizes share one. Qwen3-TTS runs on
the server's Python; Chatterbox on Python 3.13, because its dependency `spacy-pkuseg` has no
wheels for newer Pythons (`[tool.vbot.local-tts.chatterbox]` comment in `pyproject.toml`). `local-tts` installs only uv in the
development-checkout server interpreter; packaged roles ship uv. Shipped recipes install each SDK and
matched Torch/audio packages inside the managed environment. Verification writes a completion
receipt and never loads weights; once the target's Model is fetched, TTS is available
without restarting the server. A missing
interpreter or incomplete setup requires setup again. Fixed upstream revisions
avoid accidentally selecting Chatterbox's older PyPI V2 implementation.
Repeated Chatterbox setup explicitly reinstalls the PyPI distribution for dependency resolution
before restoring the pinned source without dependencies. This avoids resolving transitive Git
requirements from an already installed, same-version source distribution.

`speech_worker.py` starts without importing vBot, loads SDKs only inside its
child environment (a `{"load": true, "options"}` request loads the model and answers
`{"loaded": true}`; `_TtsEngine` construction returns only after it, like
`_ManagedSttEngine`) (every speech environment runs it, Chatterbox's on Python 3.13,
so it keeps to 3.13 syntax and standard library, which ruff and the commit hook check:
PROJECT.md -> Development -> Python version), reports `loading`/`synthesizing` phases and writes
mono PCM16 WAV to a parent-owned temporary path. When playback is requested, each
completed text chunk is also written as a separate temporary WAV; its small
`audio_chunk` control frame lets the parent forward PCM and remove that file
before the complete `done` result. Audio bytes never enter worker control frames.
Request cancellation and executor shutdown close active playback callbacks and
join their cleanup before releasing the engine. A failed playback callback ends
the worker before removing its temporary files, including a WAV still open in the
child on Windows.
The parent retains one process per TTS target
while that target's load options (`device`) match, bounds requests to 5,000 characters / 64 MiB output,
and owns timeouts and whole-process-tree cleanup (Windows launchers have child
interpreters): `_TtsEngine` and `_ManagedSttEngine` share `_WorkerProcess`, so a TTS
child that has not loaded within `_LOAD_DEADLINE_S` or answered a request within
`_SYNTHESIS_DEADLINE_S` (1800 s) is ended like an STT child (deadline, cancellation grace, shutdown above), and the
request fails with `LocalSpeechExecutionError`. Sentence/word chunking bounds each generation context. No voice
cloning input is exposed. The child loads only from the `model_path` it receives
(the installed Model directory) and disables Hub networking for SDK loading and
inference. The pinned Chatterbox tokenizer's mapping lookup resolves directly from
that directory instead of probing a nested Hub cache. Native Chatterbox watermarking remains enabled. Coverage: `test_speech_tts.py` covers
SDK calls, playable WAV chunks, process boundaries, ending a child that stops answering,
setup isolation, sizes sharing an environment with their own Models, and availability.

## Provider Wire Behavior

Provider-backed speech execution does not call the chat provider adapters. `ProviderSpeechClient` subclasses `core.providers.task_client.ProviderTaskClient`, which owns the shared plumbing (constructor tuple, `from_runtime` target resolution, auth headers, POST/classify/parse cycle, retry policy - see `providers.md`); `core/model_tasks/speech_providers.py` owns only the speech payload shapes and response parsing.

`ProviderSpeechClient.transcription_input_policy()` supplies the supported source
formats and vBot request ceilings to preparation. OpenAI's format allowlist follows
its [Audio Transcriptions reference](https://developers.openai.com/api/reference/cli/resources/audio/subresources/transcriptions/methods/create);
its [speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text)
documents a 25 MB file limit. OpenRouter's common formats follow its
[STT guide](https://openrouter.ai/docs/guides/overview/multimodal/stt), which notes
upstream-dependent support and larger JSON uploads on some routes. Unverified
OpenAI-compatible Providers retain only the existing WAV/FLAC path. A format
allowed by preparation is not a guarantee for every routed Model; Provider
rejection remains an ordinary execution error, without a second format attempt.

Evidence reviewed 2026-10-09: synthetic 1.799-second WebM/Opus and WAV/PCM16 inputs
were accepted through both raw HTTP and `ProviderSpeechClient` by
`OpenRouter:api-key`, Model `openai/gpt-4o-mini-transcribe`. This verifies transport
and decoding for those two shapes, not transcription quality or all Model/format
combinations. Direct OpenAI lacked development credentials; other formats and
routed Models have documentation coverage only.

OpenRouter STT sends Base64 JSON to `/audio/transcriptions`; an original WAV or
the WAV fallback produces:

```json
{
  "model": "openai/gpt-4o-transcribe",
  "input_audio": {
    "data": "<base64-audio>",
    "format": "wav"
  }
}
```

`language: "auto"` is omitted from the provider request. Numeric `temperature` is forwarded. The OpenRouter path consumes JSON transcription responses and does not expose the unused `response_format` option, even if a catalog advertises it (`test_model_tasks.py`). Provider-specific `provider` options are preserved for OpenRouter when present.

Executable non-OpenRouter STT targets are treated as OpenAI-compatible audio endpoints and send multipart form data to `/audio/transcriptions` with `file`, `model`, and normalized optional fields such as `language`, `prompt`, `response_format`, and `temperature`.

All speech requests honor the universal `extra_options` escape hatch: for JSON payloads (OpenRouter STT, all TTS) the object adds non-empty fields at the top level (`merge_extra_options` from `core/providers/task_client.py`); for the multipart OpenAI-compatible STT path values are stringified as form fields (booleans as lowercase literals, containers JSON-encoded). Either path rejects a collision with an authored request field locally as a non-retryable Provider error before send, including the multipart `file` upload part.

Executable TTS targets send JSON to `/audio/speech` and return raw audio bytes. TTS is billed and non-idempotent: connect/connect-timeout/pool-timeout failures and HTTP 429 remain retryable, but ambiguous read/write/remote-protocol failures, every unverified 5xx response (including 502/503/504), and empty/unusable 2xx results stop after one attempt with `ProviderOutcomeUnknownError`. No current TTS endpoint profile sends an idempotency header. `voice` is taken from stored task-model options, populated from `model.capabilities.supported_voices` when the model provides them - OpenRouter models get model-specific voice lists (e.g. Kokoro 54, Gemini TTS 30, Voxtral 30); OpenAI models get the canonical OpenAI voice list; other providers fall back to free-text `voice` input. `response_format` per provider (OpenRouter `mp3`/`pcm`; OpenAI full set `mp3`/`opus`/`aac`/`flac`/`wav`/`pcm`). Numeric `speed` stays top-level for all providers. OpenRouter receives only OpenAI speaking instructions nested under `provider.options.openai.instructions` when `instructions` is set (gated on `model.capabilities.supported_parameters`); other OpenAI-compatible providers receive `instructions` at the top level. If the provider omits `content-type`, `SpeechSynthesisResult.media_type` is derived from `response_format`; an empty success body is rejected as an unknown outcome rather than persisted as an empty artifact.

With `on_audio`, the same speech POST is consumed as an HTTP byte stream through
`ProviderTaskClient.post_and_parse(consume=...)`; endpoint payloads, authentication, Usage and retry
policy stay shared. `speech_audio.py` demuxes the selected format with PyAV on a
worker, feeds the callback mono PCM16 at the source sample rate, and retains the
original encoded bytes for the final artifact. Its input pipe is bounded to
256 KiB; encoded and decoded audio are each bounded to 64 MiB. Cancellation
closes the pipe/callback and joins the decoder. A transport or decoding failure
never creates an artifact or automatically repeats a billed synthesis.

Raw `audio/pcm` responses are wrapped in WAV for both streaming and ordinary
callers; an absent/generic content type uses the requested format. An explicit
encoded audio content type remains authoritative. Rate/channel parameters come from `Content-Type`; OpenAI may omit them
because its documented format is mono PCM16 at 24 kHz. Other Providers must
identify the rate; vBot refuses an unknown rate rather than saving misleading
audio. This follows the [OpenAI speech guide](https://developers.openai.com/api/docs/guides/text-to-speech)
and [OpenRouter's PCM cookbook example](https://openrouter.ai/docs/cookbook/audio/two-voice-podcast)
(`audio/pcm;rate=24000;channels=1`, reviewed 2026-10-09); no new Model/endpoint
compatibility claim is inferred. Tests use real in-memory WAV/MP3 decoding and
controlled HTTP streams to prove playback starts before the response finishes,
PCM artifacts retain all samples, and failed/cancelled streams release workers.

## Server & Tool Contracts

- `POST /api/speech/transcribe` accepts multipart file upload, enforces the runtime upload limit before reading into `SpeechService`, and returns `SpeechTranscriptionResult.to_dict()`.
- With `Accept: application/x-ndjson`, the same upload streams request-local
  progress heartbeats and one terminal result/error. `server/app.py` owns this
  transport and cancels/reaps work on disconnect; local worker cancellation
  waits for the running engine call, bounded for worker children (Local engines). Ordinary JSON clients remain supported.
  Coverage: `tests/server/test_speech_endpoints.py`.
- Status and install of every local speech target go through the generic
  `task_model.local_setup_status/install {target}` (`model_tasks.md` -> Contracts),
  as do `task_model.local_memory_status` and `local_unload`.
  `speech.local_setup_restart {target}` in `server/rpc/task_model_methods.py` requires
  one exact local speech target (missing, non-`local/` or unknown -> `invalid_request`),
  never client commands, package names or paths. Restart requires that target's
  `restart_required` state (else `setup_not_finished`) and the server startup callback
  (else `restart_unavailable`); its detached CLI lifecycle helper
  targets the exact running bind/data directory; a scheduled restart logs one INFO
  line. Coverage: task-model RPC and
  server-main tests.
- `speech.local_memory_status` reports each engine's loaded/busy state.
  `speech.local_unload` requires one exact `local/...` target and returns whether
  release occurred plus refreshed memory status. It never unloads other engines
  or interrupts work. Specialized Models polls status and offers one button per
  model, disabling only the busy/unloaded model. RPC and component tests cover
  targeted release, invalid requests and independent controls.
- `speech.prepare_transcription` takes no parameters and returns
  `{state}` from `SpeechService.prepare_transcription()` without waiting for the
  load. Accessors send it as a best-effort hint when a recording starts (Desktop
  Voice command recording, Desktop dictation, Chat microphone, terminal dictation)
  and ignore failures.
- With `Accept: application/x-ndjson`, synthesis uses the same progress stream
  plus a transient playback URL as soon as PCM is available, and returns a
  persisted speech artifact projection as its terminal result. The Settings
  preview consumes early playback; ordinary clients still receive raw audio.
- `POST /api/speech/synthesize` accepts JSON `{ "text": "..." }`, rejects malformed JSON or blank text before calling `SpeechService`, and returns raw audio bytes with the synthesized media type.
- `GET /api/speech/playback/{playback_id}` streams transient framed PCM, independently of progress and Run event transports; an expired resource returns HTTP 410.
- `GET /api/speech/artifacts/{artifact_id}` streams a persisted speech artifact through `FileResponse`.
- The built-in `generate_speech` Tool accepts only `text`; it returns a tool artifact payload from `SpeechArtifact.to_dict()` and intentionally exposes no model, provider, voice, format, or speed arguments.

## Artifacts

TTS tool output is stored under `<data_dir>/artifacts/speech/` through the shared `TaskArtifactStore` (`core/model_tasks/artifacts.py`): one audio file and one sidecar JSON metadata file per artifact. New artifact IDs use `aud_` plus 12 lowercase base32 characters, filenames are `<artifact_id>.<extension>`, and sidecars contain `format_version` 1, `id`, `filename`, `media_type`, and `size_bytes`. Speech artifacts are not normal attachments and are not persisted as chat messages by default. Synthesis writes the complete artifact on a worker, including durable I/O; cancellation waits for that write to finish, so it cannot abandon a partial publication or block the Event Loop with file syncs.

A sidecar is a durable JSON document under the Generation 1 contract (`settings.md` -> JSON Document Contract; registry kind `speech_artifact_metadata`, validated by `validate_task_artifact_metadata_file` for `doctor config`), written once and never rewritten. It is not a data-snapshot member: a snapshot holds no blobs. The store validates the sidecar (a missing, older or newer `format_version` or an invalid field is refused; unknown fields are ignored) and exact artifact identity before using metadata. Filenames must belong to that id inside the artifact directory; sidecars cannot redirect reads to other files or symlinks. Malformed JSON/UTF-8, invalid metadata and missing or unreadable blobs raise the ordinary Speech configuration error at this owner boundary with a generic message (`test_artifacts.py`).

## Errors

Callers of `SpeechService` should see expected speech errors as `SpeechError` subclasses (`SpeechError` derives from the shared `TaskError` base in `core/utils/errors.py`):

- `SpeechConfigurationError` for missing bindings, empty input, invalid artifact ids, and missing artifacts.
- `SpeechInputError` for malformed or unsupported recording data and exceeded input limits; `too_large` distinguishes a size/duration refusal.
- `SpeechBusyError` when all four transcription admission slots are occupied; retry after an active request finishes.
- `SpeechUnsupportedTargetError` for configured local targets with no execution adapter.
- `SpeechExecutionError` for provider/network/runtime request failures.
- `SpeechOutcomeUnknownError` for TTS requests that may have completed but cannot be safely replayed; the `generate_speech` Tool returns `provider_outcome_unknown`, `retryable: false`, plus the operation key in its message.

Missing STT bindings and Provider request failures are logged through `vbot.speech` without credentials. Provider/network failures raised inside `ProviderSpeechClient` become `SpeechExecutionError`, with the TTS unknown-outcome subtype preserved for Tool/UI/log correlation; rejected source audio remains `SpeechInputError`. The server maps invalid audio to HTTP 400, exceeded input limits to 413, full STT admission to 429, `SpeechConfigurationError` to 409, `SpeechUnsupportedTargetError` to 422, and `SpeechExecutionError` to 502. STT retains the shared historical provider retry policy; only TTS opts into the stricter non-idempotent policy.

## Constraints & Gotchas

- STT consumes complete recordings; TTS can deliver early PCM playback while retaining one complete artifact. Realtime voice sessions and partial STT streaming are outside this domain.
- Transcription decoding/conversion depends on PyAV from the `[server]` dependency group; its binary wheels carry FFmpeg support for decoding browser WebM/Opus input and encoding the WAV/PCM16 and FLAC/PCM16 fallback profiles. It runs on bounded workers outside the server Event Loop (Transcription input above).
- The built-in conversion profiles are `compatibility` (WAV, mono PCM16, 16 kHz) and `high_quality` (FLAC, mono PCM16, 48 kHz); `custom` accepts WAV or FLAC at 16, 24, or 48 kHz. These remain stored profiles, but apply only when Provider input needs conversion; accepted originals and local STT bypass them. Settings labels describe the concrete presets without implying that upsampling improves recognition. The profile is live-read per Provider preparation; the input-size limit remains restart-applied.
- Binary audio transport stays outside JSON-RPC. Accessors use dedicated HTTP endpoints for recording upload and synthesized audio download.
- The speech HTTP client is not the chat adapter stack. Provider-specific chat behavior, debug capture, streaming behavior, or message formatting changes do not automatically apply here.
- Local speech imports remain dependency-free; dependency availability does not promise GPU/model readiness. Device, checkpoint and memory errors are reported during execution as `SpeechExecutionError`; missing extras remain `SpeechUnsupportedTargetError`.
- Artifact persistence (shared `TaskArtifactStore`, `model_tasks.md`) exclusively reserves the sidecar name, then creates the audio file without replacing any file and writes the complete metadata; interrupted writes can leave invalid sidecars or orphaned audio blobs, whose names remain occupied.
- No credentials may be logged, persisted in artifacts, or returned to accessors.
