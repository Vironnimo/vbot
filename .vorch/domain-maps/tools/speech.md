# Speech Tool

Built-in `generate_speech` Tool for creating speech artifacts through the central TTS task-model binding.

## Interfaces

- Tool name: `generate_speech`
- Registration: `register_generate_speech_tool(registry, speech_service)`
- Model-facing schema: required `text` (string, `minLength: 1`) with no `additionalProperties` keyword; dispatch rejects unknown or malformed arguments. The Tool intentionally exposes only `text` - model, provider, voice, format, speed, and instructions come from Settings `model_tasks.text_to_speech`.
- Dialects (owner argument normalizer, spelling-insensitive): `input` (the OpenAI speech API name), `content`, and `transcript` -> `text`; two different texts fail with the central conflict error.
- Display: summary field `text`.
- Success data: `{ artifact }`; the artifact dict is also returned in the top-level `artifacts` list.
- Artifact shape: `{ id, kind: "speech", filename, media_type, size_bytes, url }`. `url` is a server-local speech artifact URL (`/api/speech/artifacts/<id>`), not an attachment URL.
- The model-facing `data.artifact` copy additionally carries `path` (the audio file's absolute path, rendered with the shared forward-slash Model presentation) so the agent can deliver the audio outside the web chat (e.g. via `channel_send`).
- Invalid or empty `text` returns `invalid_arguments` with an example call. Expected speech failures return `speech_error` (with `retryable: false`) instead of crashing the Run: `SpeechExecutionError` uses the shared Provider wording of `core/tools/_media_failures.py` (see `tools/image.md`), and configuration or unsupported-target errors say Text to speech is not available and tell the Agent to have the user choose a working Text to speech model in Settings → Voice → Speech models (`_media_failures.settings_place`). `provider_outcome_unknown` keeps its code and message.
- Tests: `tests/core/tools/test_speech_tool.py`.

## Runtime

Runtime registers the tool at startup with the runtime-owned `SpeechService`. The tool uses `SpeechService.synthesize_artifact()` and never calls providers directly.

The service's `on_audio` callback publishes playable PCM while inference is still running. The Tool lazily creates a bounded transient playback resource and emits one SSE-only `speech_playback` event `{tool_call_id, url}`; audio bytes never enter the Run or Session history. Cancellation and errors terminate that resource explicitly. The final result still returns the complete durable artifact for replay and delivery. Playback resource framing and lifetime belong to `model_tasks/speech.md`.

## Constraints & Gotchas

- Do not add provider/model/voice fields to the tool schema.
- The tool should remain a normal user-visible tool, not an internal tool.
- The Chat UI renders speech in the shared audio player outside the collapsible tool `<details>`; a live Run starts transient playback as soon as audio arrives, then hands replay/download to the completed artifact without replaying it automatically. A Run rebuilt from Session history shows the artifact paused. If no transient resource was available, the final artifact retains automatic playback for the live Run. Full rendering detail lives in `webui/chat.md`.
