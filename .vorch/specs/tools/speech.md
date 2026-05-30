# Speech Tool

Built-in `text_to_speech` tool for creating speech artifacts through the central
TTS task-model binding.

## Contract

Tool name: `text_to_speech`

Description: creates a speech audio artifact from text using the configured
text-to-speech model.

Provider-visible schema:

```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string",
      "minLength": 1,
      "description": "The text to synthesize."
    }
  },
  "required": ["text"],
  "additionalProperties": false
}
```

The tool intentionally exposes only `text`. Model, provider, voice, format,
speed, and instructions come from Settings `model_tasks.text_to_speech`.

## Results

Success returns a normal tool success envelope:

```json
{
  "ok": true,
  "error": null,
  "data": {
    "message": "Speech artifact created.",
    "artifact": {
      "id": "f1e2d3c4...",
      "kind": "speech",
      "filename": "f1e2d3c4....mp3",
      "media_type": "audio/mpeg",
      "size_bytes": 1234,
      "url": "/api/speech/artifacts/f1e2d3c4..."
    }
  },
  "artifacts": [
    {
      "id": "f1e2d3c4...",
      "kind": "speech",
      "filename": "f1e2d3c4....mp3",
      "media_type": "audio/mpeg",
      "size_bytes": 1234,
      "url": "/api/speech/artifacts/f1e2d3c4..."
    }
  ]
}
```

Invalid or empty `text` returns `invalid_arguments`. Expected speech failures
return `speech_error` in the tool failure envelope instead of crashing the Run.

## Runtime

Runtime registers the tool at startup with the runtime-owned `SpeechService`.
The tool uses `SpeechService.synthesize_artifact()` and never calls providers
directly.

## Constraints & Gotchas

- Do not add provider/model/voice fields to the tool schema.
- The tool should remain a normal user-visible tool, not an internal tool.
- The returned artifact URL is a server-local speech artifact URL, not an
  attachment URL.
