# Task Models

The single deep task module: bindings from specialized task types to concrete provider or local targets, plus the task execution services that run them.

## Overview

`core/model_tasks/` owns both layers of specialized task models:

1. **Bindings & discovery** (`model_tasks.py` main file plus constants/local targets/options) - normalized settings, target ID parsing, credential-gated discovery, local target descriptors, backend-owned option schemas for the Settings UI.
2. **Execution** - per-task services and their wire clients for speech, image, embeddings, video, and music. Bindings and execution change together when a task type is added - one module on purpose. Shared internals: `TaskBindingResolver` (binding lookup + options merge + target parse) and central artifact handling; image/video/music write to caller-owned directories selected by their Tools.

`options.py` owns option validation and task dispatch. Its internal `_option_types.py` holds field records and shared constructors; `_image_options.py` and `_media_options.py` build image and speech/video/music fields from existing Model facts. Public option imports and schema behavior remain unchanged.

Execution details live in child maps (`model_tasks/speech.md`, `image.md`, `embeddings.md`, `video.md`, `music.md`); the shared wire base class is `ProviderTaskClient` in `core/providers/task_client.py` (see `providers.md`). Runtime wires `TaskModelService` after providers/models/credentials/storage, then constructs the per-task execution services. Provider-backed target visibility delegates to `ModelRegistry.query()` plus usable credentials; local targets bypass catalogs/credentials but must register explicitly with `LocalTaskTargetRegistry`.

## Data Model

The fixed accessor voice companion is a specialized execution client without a configurable Task Model binding; its separate transport and app-operation contract is in `model_tasks/live.md`.

Supported task types (`constants.SUPPORTED_TASK_TYPES`): `speech_to_text`, `text_to_speech`, `image_understanding` (the text-output-from-image task behind route-gated `analyze_image`), `image_generation`, `video_generation`, `music_generation`, `text_embedding`.

Bindings persist under `model_tasks` keyed by task type: non-empty `target` + options object. Public updates are sparse (options-only updates keep the existing target; empty target removes; Storage drops the section when empty); validation runs on each changed complete resulting binding before persistence; unchanged bindings are no-ops even if live catalog changes have made their options stale, and a changed target starts options at `{}` rather than inheriting incompatible ones.

Provider target IDs use `<provider>/<model>::<connection-local-id>[:<account-id>]` (parser accepts a provider-prefixed connection suffix, persisted form uses the local connection id; a trailing account pins credential selection while listing stays connection-level). Local IDs are `local/<id>` without `/` or `::`; descriptors reject unknown task types.

## Contracts

- `binding_for(task_type)` returns the configured binding or raises on unsupported/unconfigured.
- `binding_is_usable(task_type)` is the live preflight: rejects missing/stale bindings, incompatible capabilities, forbidden connections, unusable credentials, and unrunnable local targets. Most tasks check explicit `task_types`; `image_understanding` checks its actual modality contract (`text`+`image` input, text output) because provider catalogs may advertise modalities but omit derived tags. Execution services may deepen it with wire facts (e.g. ImageService gates Chat's `analyze_image`).
- Target listing sorts by kind/label/id and skips models whose connection allowlist forbids a usable connection - restricted models never cross-product against other connections of the same provider. Public descriptors expose ids/facts only; accessors never reparse labels into connection ids.
- Option schemas resolve model-aware (`option_schema_for(..., *, model)`) falling back to conservative provider defaults without a registry hit; local targets surface descriptor-declared fields. `options_with_defaults` merges backend schema defaults under stored options before provider routing.
- `patch_options` can unset a stored option even after it disappears from the live schema; the resulting binding must still validate. Unknown unset names that were never stored still fail validation.
- Server RPCs expose settings/update/patch_options/list_targets/status/options as thin delegates; validation errors map to stable `invalid_request`. `task_model.status` is the Desktop Voice preflight.

Artifact identity is owned by `artifacts.py` and the image writer: `img_`, `aud_`, `vid_`, and `mus_` plus 12 lowercase base32 characters. Workspace files are claimed exclusively through `core/utils/ids.py`; sidecar-backed artifacts reserve the shared metadata basename before writing so collisions across extensions cannot replace metadata. Readers accept bounded safe opaque ids, preserving stored references. Collision and round-trip coverage: `tests/core/model_tasks/test_artifacts.py`, `test_image.py`.

## Conventions

- Option schemas are backend-owned render hints over Model-DB facts, never a hardcoded capability matrix. Accessors render field types generically (`text`, `textarea`, `select`, `number`, `boolean`, `json`) without provider-specific rules.
- Changed binding options validate against the resolved target schema before persistence - including generic Settings surfaces; required fields may satisfy via schema defaults.
- Per-model option **facts** live in `capabilities.task_options` (see `models.md`); this domain owns presentation only: enum -> select with leading Provider-default choice (forced-default exceptions get real values), range -> bounded number (collapsed/single-value enums skipped), boolean -> toggle or free-value number, string -> text. Runtime parameters and redundant shorthand fields never render. Models without facts fall back to conservative provider-level schemas.
- Two pseudo-options: `provider_options` (passthrough rendered only when advertised, sent nested as `provider.options`) and universal `extra_options` escape hatch (adds non-empty provider-specific fields only; collisions with task-authored request fields fail locally as non-retryable Provider errors before send - helpers shared via the task client).
- Wire shaping belongs to per-task wire clients; add a new workflow in order: confirm the task type constant, ensure discovery produces matching capability tags, add option fields only if the UI needs them, implement service + wire client pair, add a child map.

## Constraints & Gotchas

- The binding/discovery layer never calls media APIs or shapes wires; execution modules resolve bindings only through `TaskModelService`, never reading `settings.json`.
- Missing targets usually mean missing credentials or stale catalogs - refresh the Model DB after configuring keys instead of hand-editing generated files.
- Video/Music currently require OpenRouter (details in their child maps). Runtime registers STT `local/qwen3-asr` / `local/parakeet` and TTS `local/qwen3-tts` / `local/chatterbox` from the optional speech executor's catalog. Descriptors require a live availability callback for `usable`; registration alone does not imply an executable target. Imports and preflight never load ML runtimes or weights. Covered by `test_model_tasks.py` and `test_speech_local.py`.
- `audio_generation` is a capability, not a configurable binding - generic audio output must not route as TTS or Music.
- Loaded `task_options` freeze into read-only views/tuples; schema builders accept both sequence forms.

## References

- Changing GPT-Live session creation, spoken app operation, or proactive Agent announcements -> `model_tasks/live.md`
