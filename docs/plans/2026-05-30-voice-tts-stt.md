## Plan: Specialized Task Models, Voice First

**Goal:** vBot will have a central task-model settings architecture that first powers speech-to-text and text-to-speech, while leaving the same path open for image generation, image editing, and video generation.

**Context:** The immediate product need is voice: a microphone button in Chat should transcribe speech into composer text, and agents should later be able to speak through a TTS tool. The architectural need is wider than voice. The model catalog already exposes task filters such as `speech_to_text`, `text_to_speech`, `image_generation`, `image_edit`, and `video_generation`; the missing layer is a user-selected binding from each task type to one concrete target plus provider-specific options.

OpenAI and Mistral confirm why task-specific option schemas must be dynamic rather than hardcoded. OpenAI STT supports file-oriented transcriptions and translations, model-dependent response formats, prompts, timestamps, diarization, streaming, and a 25 MB upload limit. OpenAI TTS exposes model, input, voice, instructions, response format, streaming audio, built-in voices, and custom voice IDs. Mistral STT exposes language, timestamps, diarization, context bias, temperature, file/file_id/file_url, and streaming; Mistral TTS exposes saved voice IDs or one-off reference audio. Browser recording should use `MediaRecorder`/`getUserMedia`, with MIME type detection because browser audio containers differ.

References checked:
- OpenAI Speech to text: https://developers.openai.com/api/docs/guides/speech-to-text
- OpenAI Text to speech: https://developers.openai.com/api/docs/guides/text-to-speech
- Mistral Audio and Transcription: https://docs.mistral.ai/capabilities/audio/
- Mistral Audio Transcriptions API: https://docs.mistral.ai/api/endpoint/audio/transcriptions
- Mistral Speech Generation: https://docs.mistral.ai/capabilities/audio/text_to_speech/speech
- MDN MediaRecorder: https://developer.mozilla.org/docs/Web/API/MediaRecorder

**Requirements:**
- Central place to configure STT and TTS models and their options.
- The selected STT/TTS settings are used everywhere speech is needed.
- Chat gets a microphone button so the user can speak instead of typing.
- The agent later gets a TTS tool, using the central TTS settings.
- Local engines such as Whisper must fit the same architecture.
- The design should generalize to future image and video task models.

**Scope:**
- In: Generic task-model bindings, STT/TTS settings, target listing, option schemas, file-based STT endpoint, file-producing TTS endpoint, Chat microphone integration, agent TTS tool, docs/spec updates.
- In: Local target abstraction design and minimal registry hooks for local Whisper-style targets.
- Out: Realtime conversational audio, live partial microphone transcripts, voice cloning management UI, image/video execution, new third-party dependencies unless explicitly approved.
- Out: Persisting generated audio as normal chat messages by default. TTS artifacts are execution artifacts unless a later product decision promotes them into timeline content.

**Final size:** Large. This is a new cross-cutting architecture touching settings, runtime wiring, server transport, WebUI, tools, model/provider integration, and specs.

### Architecture Decisions

| Decision | Chosen approach | Rejected alternative | Reason |
|---|---|---|---|
| Central abstraction | Add `core/model_tasks/` for task-model bindings and option schemas | Put everything under `core/speech/` | Speech is the first use case, not the architecture boundary. Image/video should reuse the same binding and settings flow. |
| Settings key | Store `model_tasks` in `settings.json` keyed by task type | Store `voice.stt` and `voice.tts` | Task keys match the existing `Model.capabilities.task_types` vocabulary and scale to image/video. |
| Execution services | Keep execution in task-specific services such as `core/speech/` | Make `core/model_tasks/` execute all media tasks | Bindings/options are shared, but STT, TTS, image, and video have different I/O and transport concerns. |
| Target identity | Support provider targets and local targets through one `TaskModelTarget` shape | Force local engines into provider/model IDs | Local Whisper/Piper-style targets may not have provider credentials or model catalog files. The UI should still present them uniformly. |
| Options | Backend returns per-task/per-target option schemas | WebUI hardcodes voice/language/format fields | Provider APIs differ and change. The backend should own valid options and defaults. |
| Audio transport | Dedicated HTTP endpoints for audio blobs | JSON-RPC with base64 audio | Existing server contract keeps blobs outside RPC via upload endpoints. Audio should follow that pattern. |
| First STT UX | Push-to-talk file recording, then transcription result | Realtime streaming transcription | File STT is smaller, provider-portable, testable, and enough for the chat microphone button. Realtime can be Phase 2 later. |

### Data Model

`settings.json` should gain a normalized `model_tasks` section:

```json
{
  "model_tasks": {
    "speech_to_text": {
      "target": "openai/gpt-4o-transcribe::api-key",
      "options": {
        "language": "auto",
        "response_format": "json"
      }
    },
    "text_to_speech": {
      "target": "openai/gpt-4o-mini-tts::api-key",
      "options": {
        "voice": "alloy",
        "response_format": "mp3",
        "speed": 1.0
      }
    }
  }
}
```

Planned public shapes:

```python
TaskModelBinding = {
    "task_type": "speech_to_text",
    "target": "openai/gpt-4o-transcribe::api-key",
    "options": {"language": "auto"}
}

TaskModelTarget = {
    "id": "openai/gpt-4o-transcribe::api-key",
    "kind": "provider",
    "provider_id": "openai",
    "model_id": "gpt-4o-transcribe",
    "connection_id": "openai:api-key",
    "label": "OpenAI / GPT-4o Transcribe",
    "task_types": ["speech_to_text"],
    "usable": true
}

TaskModelOptionSchema = {
    "fields": [
        {
            "name": "language",
            "type": "select",
            "label": "Language",
            "default": "auto",
            "options": [{"value": "auto", "label": "Auto"}],
            "required": true
        }
    ]
}
```

Keep these shapes provider-neutral. Provider-specific details belong behind target option providers or execution adapters.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Task-model foundation | `model_tasks` settings parse/validate/load/save path, target listing, option schema RPCs, specs updated. |
| M2 | Speech execution | STT/TTS execution service with OpenAI-compatible first implementation path and local-target extension points. |
| M3 | Voice UI | Settings panel for STT/TTS bindings and Chat microphone button using the central STT binding. |
| M4 | Agent TTS | Agent-visible TTS tool that uses the central `text_to_speech` binding only. |
| M5 | Hardening | Quality gates, docs alignment, live verification path, and follow-up notes for realtime/image/video. |

### Phase Breakdown

#### Phase 1: Task-Model Settings Foundation

**Goal of this phase:** Persist and validate task-model bindings without executing any media task yet.

**Can run in parallel with:** none, because settings schema and storage are shared.

- Define `core/model_tasks/` public data classes, constants for supported initial task types, target ID parsing helpers, option-schema types, and validation errors - read: [.vorch/specs/models.md, .vorch/specs/settings.md, .vorch/specs/providers.md], files: [core/model_tasks/__init__.py, core/model_tasks/model_tasks.py, tests/core/model_tasks/test_model_tasks.py]
- Extend public settings update parsing for `model_tasks`, including sparse per-task updates, target string validation, and JSON-object options - files: [core/settings/settings.py, tests/core/settings/test_settings.py]
- Extend raw settings validation to recognize and validate `settings.model_tasks` - files: [core/settings/validation.py, tests/core/settings/test_settings.py]
- Add storage helpers `load_model_task_settings()` and `update_model_task_settings()` that normalize missing sections to empty bindings and preserve unrelated settings - files: [core/storage/storage.py, core/storage/__init__.py, tests/core/storage/test_storage.py]
- Update specs and project docs for the new domain and settings contract - files: [.vorch/PROJECT.md, .vorch/specs/settings.md, .vorch/specs/model_tasks.md]

**Dependencies:** Existing settings/storage patterns.

**Done when:**
- `settings.update({ model_tasks: ... })` parses valid bindings and rejects malformed task names, non-string targets, and non-object options.
- `validate_settings_file()` reports errors for malformed `model_tasks`.
- Storage round-trips `model_tasks` without rewriting unrelated settings.
- `python scripts/quality.py core/model_tasks core/settings core/storage tests/core/model_tasks tests/core/settings tests/core/storage` passes.

#### Phase 2: Target Discovery and Option Schemas

**Goal of this phase:** Let clients ask which targets can serve a task and which options apply to a selected target.

**Can run in parallel with:** Phase 3 backend execution adapter research only after Phase 1 data shapes are stable.

- Implement `TaskModelService` target discovery from provider models with usable connections, filtered by `Model.capabilities.task_types` - read: [.vorch/specs/models.md, .vorch/specs/providers.md, .vorch/specs/runtime.md], files: [core/model_tasks/model_tasks.py, tests/core/model_tasks/test_model_tasks.py]
- Add local target registry hooks with no hard dependency on Whisper yet, using static descriptors that can be empty until local engines are configured - files: [core/model_tasks/local_targets.py, core/model_tasks/__init__.py, tests/core/model_tasks/test_local_targets.py]
- Add option schema providers for initial OpenAI and Mistral STT/TTS targets based on provider/model metadata and conservative known defaults - files: [core/model_tasks/options.py, tests/core/model_tasks/test_options.py]
- Wire `Runtime` with `model_tasks` service after providers, models, credentials, and storage are loaded - files: [core/runtime/runtime.py, core/runtime/__init__.py, .vorch/specs/runtime.md, tests/core/runtime/test_runtime.py]
- Add RPC delegates: `task_model.settings`, `task_model.update`, `task_model.list_targets`, and `task_model.options` - read: [.vorch/specs/server.md], files: [server/delegates.py, tests/server/test_task_model_delegates.py]
- Update server and model task specs with RPC payloads - files: [.vorch/specs/server.md, .vorch/specs/model_tasks.md]

**Dependencies:** Phase 1.

**Done when:**
- `task_model.list_targets({ "task_type": "speech_to_text" })` returns usable provider-backed model targets filtered by task type.
- `task_model.options({ "task_type": "text_to_speech", "target": "..." })` returns a backend-owned schema.
- Missing credentials exclude provider-backed targets the same way `model.list` does.
- `python scripts/quality.py core/model_tasks core/runtime server tests/core/model_tasks tests/core/runtime tests/server/test_task_model_delegates.py` passes.

#### Phase 3: Speech Execution Service

**Goal of this phase:** Execute file-based STT and TTS through the central task-model bindings.

**Can run in parallel with:** Phase 4 frontend helper preparation after RPC contracts are stable.

- Flesh out `core/speech/` with provider-neutral request/result types, `SpeechService.transcribe()` and `SpeechService.synthesize()`, expected error classes, and logging through `vbot.speech` - read: [.vorch/specs/model_tasks.md, .vorch/specs/providers.md, .vorch/specs/attachments.md], files: [core/speech/__init__.py, core/speech/speech.py, .vorch/specs/speech.md, tests/core/speech/test_speech.py]
- Add provider speech adapter boundary for OpenAI-compatible audio endpoints without changing chat adapters. Start with HTTP-level methods for `/audio/transcriptions` and `/audio/speech` using existing provider credential resolution and retry/error conventions - files: [core/speech/providers.py, tests/core/speech/test_providers.py]
- Add Mistral speech execution support only if it can be implemented through current provider config without new dependency; otherwise leave a documented option schema and expected unsupported execution error - files: [core/speech/providers.py, tests/core/speech/test_providers.py, .vorch/specs/speech.md]
- Add local speech target execution interface for future Whisper/Piper/etc. and implement a no-target configured path that returns a meaningful expected error - files: [core/speech/local.py, tests/core/speech/test_local.py]
- Wire `Runtime.speech` after `model_tasks` and provider credentials are ready - files: [core/runtime/runtime.py, .vorch/specs/runtime.md, tests/core/runtime/test_runtime.py]
- Add dedicated server endpoints `POST /api/speech/transcribe` and `POST /api/speech/synthesize` for multipart audio/text requests and raw/audio artifact responses. Keep binary payloads out of JSON-RPC - read: [.vorch/specs/server.md, .vorch/specs/attachments.md], files: [server/app.py, server/speech.py, tests/server/test_speech_endpoints.py]
- Update specs for Speech and Server endpoint contracts - files: [.vorch/specs/speech.md, .vorch/specs/server.md, .vorch/PROJECT.md]

**Dependencies:** Phase 1 and enough of Phase 2 to resolve the active binding.

**Done when:**
- Calling `SpeechService.transcribe()` with no configured STT binding raises an expected, user-meaningful error.
- A configured OpenAI STT binding produces a normalized `{ text, language?, segments?, usage? }` result in tests using mocked HTTP.
- A configured OpenAI TTS binding produces bytes plus media type in tests using mocked HTTP.
- Server endpoints map expected speech errors to stable HTTP responses and never log credentials.
- `python scripts/quality.py core/speech core/runtime server tests/core/speech tests/core/runtime tests/server/test_speech_endpoints.py` passes.

#### Phase 4: Settings UI for Specialized Models

**Goal of this phase:** Add a Settings panel where users configure STT/TTS through the generic task-model APIs.

**Can run in parallel with:** Phase 5 Chat microphone UI after shared frontend helpers are stable.

- Add frontend API wrappers for task model RPCs and speech endpoints - read: [.vorch/specs/webui.md, .vorch/specs/server.md], files: [webui/src/lib/api.js, webui/src/lib/__tests__/api.test.js]
- Add pure Settings helpers for task-model state, target normalization, option-schema rendering state, update payloads, and dirty checks - files: [webui/src/lib/taskModelSettings.js, webui/src/lib/__tests__/taskModelSettings.test.js]
- Add a `Models` or `Specialized Models` settings sub-panel that initially renders Speech to Text and Text to Speech rows, using backend target lists and backend option schemas - read: [.vorch/DESIGN.md, .vorch/specs/webui.md], files: [webui/src/components/SettingsView.svelte, webui/src/components/__tests__/SettingsView.test.js]
- Add i18n keys for all visible copy - files: [webui/src/lib/i18n.js, webui/src/lib/__tests__/i18n.test.js]
- Update WebUI spec for the new panel and helper module - files: [.vorch/specs/webui.md]

**Dependencies:** Phase 2 RPCs.

**Done when:**
- Settings loads current `model_tasks`, lists STT/TTS targets, renders target-specific options, and saves through the central task-model update path.
- The panel follows Toasted Settings layout conventions and uses i18n for all visible text.
- `python scripts/quality-frontend.py webui/src/lib/taskModelSettings.js webui/src/components/SettingsView.svelte webui/src/lib/api.js` passes.

#### Phase 5: Chat Microphone Button

**Goal of this phase:** Let the user record speech from the browser and insert the transcript into the Chat composer.

**Can run in parallel with:** Phase 4 after `webui/src/lib/api.js` speech wrappers exist, with no file overlap except agreed API wrapper completion.

- Add a browser recording helper around `navigator.mediaDevices.getUserMedia` and `MediaRecorder`, including MIME type selection and cleanup of media tracks - read: [.vorch/specs/webui.md], files: [webui/src/lib/audioRecorder.js, webui/src/lib/__tests__/audioRecorder.test.js]
- Add microphone button states to ChatComposer: idle, requesting permission, recording, transcribing, error. It should upload the recorded blob to `/api/speech/transcribe` and append or replace composer text based on current input state - read: [.vorch/DESIGN.md, .vorch/specs/webui.md], files: [webui/src/components/ChatComposer.svelte, webui/src/components/__tests__/ChatComposer.test.js]
- Surface transcription failures via existing toast/action feedback rather than inline permanent UI - files: [webui/src/components/ChatView.svelte, webui/src/components/__tests__/ChatView.test.js]
- Update WebUI spec with microphone behavior and cleanup requirements - files: [.vorch/specs/webui.md]

**Dependencies:** Phase 3 endpoint and Phase 4 API wrapper.

**Done when:**
- Microphone recording stops all media tracks on cancel, submit, component destroy, and error.
- Successful transcription inserts text into the composer without sending automatically.
- Unsupported browser APIs and missing STT configuration produce meaningful UI feedback.
- `python scripts/quality-frontend.py webui/src/lib/audioRecorder.js webui/src/components/ChatComposer.svelte webui/src/components/ChatView.svelte` passes.

#### Phase 6: Agent TTS Tool

**Goal of this phase:** Let an agent speak through a normal tool that always uses the central TTS binding.

**Can run in parallel with:** none, because tool registration touches runtime/tool docs and speech service.

- Add `speak` or `text_to_speech` built-in tool with a minimal schema such as `{ text }`; do not expose model, voice, provider, or format tool arguments - read: [.vorch/specs/tools.md, .vorch/specs/speech.md], files: [core/tools/speech.py, core/tools/__init__.py, tests/core/tools/test_speech_tool.py]
- Wire the tool into Runtime registration with the existing built-in tool patterns - files: [core/runtime/runtime.py, tests/core/runtime/test_runtime.py]
- Return stable result envelopes containing artifact metadata or playback URL, not raw audio bytes - files: [core/tools/speech.py, tests/core/tools/test_speech_tool.py]
- Update tool specs and prompt/tool list expectations - files: [.vorch/specs/tools.md, .vorch/specs/tools/speech.md, .vorch/specs/speech.md]

**Dependencies:** Phase 3.

**Done when:**
- The tool fails gracefully when TTS is not configured.
- The tool succeeds with mocked `SpeechService.synthesize()` and returns a stable artifact result.
- The tool definition does not let the model override central TTS model/voice settings.
- `python scripts/quality.py core/tools core/runtime tests/core/tools/test_speech_tool.py tests/core/runtime/test_runtime.py` passes.

#### Phase 7: Full Verification and Documentation Alignment

**Goal of this phase:** Finish the feature as a coherent product slice.

**Can run in parallel with:** none.

- Run full backend and frontend gates - files: [scripts/quality.py, scripts/quality-frontend.py]
- If browser-visible claims are made, start the app only through `python scripts/test-env.py start`, verify Settings and Chat microphone flows, capture screenshot evidence, then stop with `python scripts/test-env.py stop` - read: [.vorch/TESTER.md], files: [no source edits expected]
- Final docs pass for architecture, settings, speech, model tasks, server, webui, runtime, and tools - files: [.vorch/PROJECT.md, .vorch/specs/model_tasks.md, .vorch/specs/speech.md, .vorch/specs/settings.md, .vorch/specs/server.md, .vorch/specs/webui.md, .vorch/specs/runtime.md, .vorch/specs/tools.md]

**Dependencies:** Phases 1-6.

**Done when:**
- `python scripts/quality.py` passes.
- `python scripts/quality-frontend.py` passes.
- Live test, if performed, includes Settings target selection and Chat microphone transcript insertion.
- Specs match the implemented behavior and public contracts.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Provider audio APIs do not fit existing chat adapter abstraction | High | Medium | Keep speech execution adapters separate from chat adapters, sharing credentials and HTTP utilities only. |
| Option schemas become too generic and hard to render | Medium | Medium | Start with a small field vocabulary: text, textarea, select, multiselect, number, boolean. Add richer field types only when needed. |
| Local Whisper adds heavy dependencies | High | Medium | Design local target interfaces now, but do not add runtime dependencies until the user approves a concrete local backend. |
| Browser audio MIME support differs across Chrome, Firefox, Safari | High | Medium | Probe `MediaRecorder.isTypeSupported()` and send actual blob MIME type to the server. Server validates and returns clear unsupported-media errors. |
| Realtime STT pressure expands scope | Medium | High | Explicitly ship file-based push-to-talk first. Realtime gets a later plan using separate streaming contracts. |
| Settings UI becomes too large | Medium | Medium | Add a `Specialized Models` sub-panel instead of scattering controls across Chat and Providers. Use shared helper modules to keep `SettingsView.svelte` from absorbing business logic. |
| Generated audio storage blurs with attachments | Medium | Medium | Treat TTS output as speech artifact first. Only promote to AttachmentStore if the product wants persisted/downloadable audio. |
| Model catalog task facts are sparse for local providers | Medium | Medium | Provider-backed target discovery uses strict task filters, but local targets are registered through explicit descriptors so sparse provider catalogs do not block local speech. |

### Assumptions & Constraints

- The first implementation should use file-based STT/TTS, not realtime voice sessions.
- No new dependency is planned for Phase 1-5. Local Whisper execution or audio transcoding may require later approval.
- OpenAI-compatible audio endpoints should be implemented through existing `httpx` and provider credential resolution rather than the OpenAI SDK.
- Mistral support should be implemented only if current provider configuration gives enough base URL and credential information; otherwise leave a clear unsupported execution path while still supporting option schemas and target selection.
- User-visible UI strings must go through `webui/src/lib/i18n.js`.
- The full app remains local-first and single-user; no new auth layer is introduced for microphone or speech endpoints.

### New Dependencies

None for the planned first slice. Possible later dependencies, requiring user approval:

- `openai-whisper`, `faster-whisper`, or `whisper.cpp` binding - local STT execution.
- `ffmpeg`/`pydub` or equivalent - server-side audio chunking/transcoding if browser/provider formats need normalization.
- A local TTS engine package - local `text_to_speech` targets.

### Follow-Up Plans

- Realtime STT and voice-agent sessions using a dedicated low-latency transport.
- Image generation and image edit execution using the same `model_tasks` binding architecture.
- Video generation execution and artifact lifecycle.
- Custom voice management UI, consent handling, and saved voice catalog refresh.
