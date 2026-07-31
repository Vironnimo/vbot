# Speech Tool

Built-in `text_to_speech` tool for creating speech artifacts through the central TTS task-model binding.

## Interfaces

- Tool name: `text_to_speech`
- Registration: `register_text_to_speech_tool(registry, speech_service)`
- Model-facing schema: required `text` (string, `minLength: 1`) with no `additionalProperties` keyword; the handler still rejects unknown or malformed arguments. The Tool intentionally exposes only `text` — model, provider, voice, format, speed, and instructions come from Settings `model_tasks.text_to_speech`.
- Display: summary field `text`.
- Success data: `{ message, artifact }`; the artifact dict is also returned in the top-level `artifacts` list.
- Artifact shape: `{ id, kind: "speech", filename, media_type, size_bytes, url }`. `url` is a server-local speech artifact URL (`/api/speech/artifacts/<id>`), not an attachment URL.
- The model-facing `data.artifact` copy additionally carries `path` (the audio file's absolute path, also named in `data.message`) so the agent can deliver the audio outside the web chat (e.g. via `channel_send`). The UI-facing `artifacts` payload stays path-free — the WebUI renders from `url`.
- Invalid or empty `text` returns `invalid_arguments`. Expected speech failures return `speech_error` instead of crashing the Run.

## Runtime

Runtime registers the tool at startup with the runtime-owned `SpeechService`. The tool uses `SpeechService.synthesize_artifact()` and never calls providers directly.

## Constraints & Gotchas

- Do not add provider/model/voice fields to the tool schema.
- The tool should remain a normal user-visible tool, not an internal tool.
- The Chat UI auto-renders a `kind: "speech"` artifact as an autoplaying `<audio controls>` element outside the collapsible tool `<details>` so the spoken reply plays immediately; full rendering detail lives in `webui.md`.
