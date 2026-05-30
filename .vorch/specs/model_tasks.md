# Model Tasks

Central bindings from specialized task types to concrete provider or local
targets. This domain chooses what model or engine should perform a task; task
execution stays in task-specific domains such as `core/speech/`.

## Overview

`core/model_tasks/` owns normalized task-model settings, target ID parsing,
target discovery, local target hooks, and backend-owned option schemas. It is
the shared architecture for speech-to-text, text-to-speech, and future media
tasks such as image generation, image editing, and video generation.

The service is wired by Runtime after providers, models, credentials, and
storage are available. It reads and writes `settings.json` through
`StorageManager` and lists only provider-backed targets whose selected
connection has usable credentials.

## Task Types

Initial supported task types are:

- `speech_to_text`
- `text_to_speech`
- `image_generation`
- `image_edit`
- `video_generation`

These values match `Model.capabilities.task_types`. Provider catalogs remain the
source for provider-backed target visibility; local targets use explicit local
descriptors.

## Settings

`settings.json` may contain:

```json
{
  "model_tasks": {
    "speech_to_text": {
      "target": "openrouter/openai/gpt-4o-transcribe::api-key",
      "options": {
        "language": "auto",
        "temperature": 0
      }
    },
    "text_to_speech": {
      "target": "openrouter/openai/gpt-4o-mini-tts-2025-12-15::api-key",
      "options": {
        "voice": "alloy",
        "response_format": "mp3",
        "speed": 1.0,
        "instructions": ""
      }
    }
  }
}
```

Each binding has a non-empty `target` and an `options` object. Sparse public
updates are allowed. Sending an empty target for a task clears that binding from
storage.

## Target IDs

Provider target IDs use:

```text
<provider-id>/<model-id-at-provider>::<connection-local-id>
```

The connection suffix may also be passed with a provider prefix, but persisted
public IDs use the local connection id, for example `::api-key`.

Local target IDs use:

```text
local/<local-id>
```

Local IDs cannot contain `/` or `::`.

## Public Shapes

`TaskModelBinding.to_dict()` returns:

```json
{
  "target": "openrouter/openai/gpt-4o-transcribe::api-key",
  "options": {
    "language": "auto"
  }
}
```

`TaskModelTarget.to_dict()` returns:

```json
{
  "id": "openrouter/openai/gpt-4o-transcribe::api-key",
  "kind": "provider",
  "provider_id": "openrouter",
  "model_id": "openai/gpt-4o-transcribe",
  "connection_id": "openrouter:api-key",
  "connection_label": "API key",
  "label": "OpenRouter / GPT-4o Transcribe",
  "task_types": ["speech_to_text"],
  "usable": true,
  "metadata": {}
}
```

`TaskModelOptionSchema.to_dict()` returns:

```json
{
  "task_type": "text_to_speech",
  "target": "openrouter/openai/gpt-4o-mini-tts-2025-12-15::api-key",
  "fields": [
    {
      "name": "voice",
      "type": "select",
      "label": "Voice",
      "default": "alloy",
      "required": true,
      "options": [{ "value": "alloy", "label": "Alloy" }]
    }
  ]
}
```

Supported option field types are small renderable primitives: `text`,
`textarea`, `select`, `number`, and `boolean`. Future fields should remain
backend-owned so accessors do not hardcode provider-specific option rules.

## Interfaces

- `validate_task_type(task_type) -> str`
- `parse_task_model_target_id(target) -> TaskModelTargetRef`
- `public_provider_target_id(provider_id, model_id, local_connection_id) -> str`
- `TaskModelService.settings() -> dict`
- `TaskModelService.update(model_tasks) -> dict`
- `TaskModelService.binding_for(task_type) -> TaskModelBinding`
- `TaskModelService.list_targets(task_type) -> list[TaskModelTarget]`
- `TaskModelService.options(task_type, target) -> TaskModelOptionSchema`
- `TaskModelService.options_with_defaults(binding) -> dict`
- `LocalTaskTargetRegistry.register(descriptor)` registers future local engines.

## RPC

Server delegates expose:

- `task_model.settings` -> `{ model_tasks }`
- `task_model.update` with `{ model_tasks }` -> `{ model_tasks }`
- `task_model.list_targets` with `{ task_type }` -> `{ targets }`
- `task_model.options` with `{ task_type, target }` -> `{ schema }`

Task-model errors are expected domain errors and map to stable request errors at
the server boundary.

## Constraints & Gotchas

- `core/model_tasks/` must not execute media tasks. Speech execution belongs in
  `core/speech/`; future image/video execution should get their own domains.
- Provider-backed discovery is credential-gated and strictly filtered by
  `Model.capabilities.task_types`.
- Generated provider catalogs may be stale. If a newly released OpenRouter
  speech model is missing from the UI target list, refresh the model database
  after configuring the OpenRouter API key.
- Local target hooks are intentionally dependency-free. Do not add Whisper,
  Piper, ffmpeg, or other engine dependencies without explicit approval.
- Option schemas are conservative provider defaults, not a complete claim about
  every model. Execution adapters still own final wire shaping.
