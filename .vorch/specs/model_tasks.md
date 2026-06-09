# Task Model Bindings

Central bindings from specialized task types to concrete provider or local targets. This domain chooses what model or engine should perform a task; task execution stays in task-specific domains such as `core/speech/` and `core/image/`.

## Overview

`core/model_tasks/` owns normalized task-model settings, task target ID parsing, credential-gated target discovery, local target descriptors, and backend-owned option schemas for the Settings UI. It is the shared binding layer behind specialized models such as speech-to-text, text-to-speech, image generation, text embedding, and future video generation. It does not call provider media APIs, create artifacts, upload files, or decide provider wire payloads; execution services resolve a binding here and then route through their own domain.

Runtime wires `TaskModelService` after providers, models, credentials, and storage are available. Provider-backed target visibility delegates to `ModelRegistry.query()` (the shared capability/task filter in `core/models/query.py`) plus usable provider credentials. Local targets bypass provider catalogs and credentials, but must be registered explicitly with `LocalTaskTargetRegistry`.

## Data Model

Supported binding task types are defined by `SUPPORTED_TASK_TYPES` in `core/model_tasks/constants.py`: `speech_to_text`, `text_to_speech`, `image_generation`, `text_embedding`, and `video_generation`.

`settings.json` stores task-model bindings under `model_tasks`, keyed by supported task type. Each persisted binding has a non-empty `target` string and an `options` JSON object. Public settings updates are sparse: sending only `options` updates the existing target when one is already persisted, sending an empty `target` removes that task binding, and `StorageManager` removes the whole `model_tasks` section when no bindings remain.

Provider target IDs use `<provider-id>/<model-id-at-provider>::<connection-local-id>`. The parser also accepts a provider-prefixed connection suffix such as `::openrouter:api-key`, but persisted public IDs use the local connection id form such as `::api-key`.

Local target IDs use `local/<local-id>`. Local IDs cannot contain `/` or `::`; descriptor validation rejects any advertised task type outside `SUPPORTED_TASK_TYPES`.

## Interfaces

- `validate_task_type(task_type) -> str` returns a supported task type or raises `TaskModelValidationError` with the current allowed vocabulary.
- `TaskModelService.settings()` returns normalized persisted bindings from `StorageManager.load_model_task_settings()`.
- `TaskModelService.update(model_tasks)` persists sparse binding updates through `StorageManager.update_model_task_settings()` and returns the normalized full section.
- `TaskModelService.binding_for(task_type)` returns a configured `TaskModelBinding` or raises `TaskModelError` when the task is unsupported or unconfigured.
- `TaskModelService.list_targets(task_type)` returns provider and local `TaskModelTarget` descriptors for one supported task type, sorted by kind, label, and id. Target expansion iterates usable provider connections, but skips a connection for a model when `model.connections` is non-empty and the connection id is not in the list — connection-restricted models (e.g. OpenAI Codex models tagged `["subscription"]`) do not cross-product against other usable connections of the same provider.
- `TaskModelTarget.to_dict()` returns public descriptors with `id`, `kind`, provider/model/connection fields, `label`, `task_types`, `usable`, and `metadata`; accessors should not infer provider connection ids by reparsing labels.
- `TaskModelService.options(task_type, target)` resolves the target's `Model` from `ModelRegistry.get(provider_id, model_id)` and passes it to `option_schema_for` to produce model-aware option schemas. Falls back to provider-level conservative defaults when the model is not found in the registry. Local targets return the descriptor's `option_fields` (default empty; reserved for future user-configured engines).
- `TaskModelService.options_with_defaults(binding)` merges backend schema defaults under stored binding options; execution domains call this before provider routing.
- `parse_task_model_target_id(target)` parses provider and local public IDs into `TaskModelTargetRef`; nested provider model IDs such as `openrouter/openai/gpt-4o-transcribe::api-key` are valid.
- `public_provider_target_id(provider_id, model_id, local_connection_id)` creates the settings-facing provider target id.
- `LocalTaskTargetRegistry.register(descriptor)` registers or replaces a future local engine descriptor without touching provider catalogs.

Server RPC delegates in `server/rpc/settings_methods.py` expose `task_model.settings`, `task_model.update`, `task_model.list_targets`, and `task_model.options`, returning `{ model_tasks }`, `{ targets }`, and `{ schema }` wrappers. Task-model domain errors are expected errors and map to stable `invalid_request` responses at the server boundary.

## Conventions

Option schemas are backend-owned render hints, not a provider capability matrix. Accessors should render `text`, `textarea`, `select`, `number`, `boolean`, and `json` fields generically and must not hardcode provider-specific option rules. The `json` field type accepts free-form JSON (arrays/objects/primitives) rendered as a monospace textarea with inline validation; the value round-trips as the parsed JSON structure.

Execution domains own final option interpretation and wire shaping. `core/model_tasks/options.py` provides conservative defaults, while `core/speech/`, `core/image/`, and provider-specific clients decide which options are sent to which API.

Add a new specialized workflow in this order: add/confirm the task type in `core/model_tasks/constants.py`, ensure provider model discovery produces matching `Model.capabilities.task_types`, add option fields in `core/model_tasks/options.py` only if the Settings UI needs them, then implement execution in the task domain. Do not add task execution code to `core/model_tasks/`.

## Constraints & Gotchas

- `core/model_tasks/` must not execute media tasks. Speech execution belongs in `core/speech/`; image generation belongs in `core/image/`; future video or image-edit execution should get their own domains.
- Provider-backed discovery is credential-gated and strictly filtered through `ModelRegistry.query()` by task type. If a target is missing from Settings, first check provider credentials and the generated model catalog before changing UI code.
- Generated provider catalogs may be stale. If a newly released OpenRouter speech or image model is missing from the target list, refresh the model database after configuring the provider API key; do not hand-edit `resources/models/<provider>.json` as a durable fix.
- The Settings UI currently renders rows for `speech_to_text`, `text_to_speech`, `image_generation`, and `text_embedding`. `video_generation` is accepted by backend validation and discovery, but has no complete UI/execution workflow yet.
- Local target hooks are intentionally dependency-free. Do not add Whisper, Piper, ffmpeg, or other engine dependencies without explicit approval.
- `LocalTaskTargetDescriptor.option_fields` is a tuple of `TaskModelOptionField` values that the local target's option schema surfaces. Descriptors are currently registered with an empty fields tuple; future user-configured engines will construct descriptors with their own option fields from persisted settings. The registry itself stays empty until a user-config plan lands — no engine is pre-built here.
- `audio_generation` is a model capability task type in `core/models/`, but not a configurable task-model binding. Generic audio output must not be routed as `text_to_speech` unless the model also advertises `speech` output and receives the `text_to_speech` task type.
- Option schemas are now model-aware: `option_schema_for(task_type, provider_id, target, *, model)` receives the resolved `Model` (capabilities + metadata + model_id) and builds model-specific fields, value sets, and profiles. Authored render-hint profiles (Recraft/Sourceful image families, aspect-ratio/size exceptions, seed gating) live in `core/model_tasks/options.py`; discoverable facts (`supported_voices`, `supported_parameters`) read from the `Model`. Voice selection for TTS uses `model.capabilities.supported_voices` when non-empty, falling back to provider-level choices (OpenAI canonical voices only for OpenAI, free-text input otherwise).
