# Task Models

The single deep task module: bindings from specialized task types to concrete provider or local targets, plus the task execution services that run them.

## Overview

`core/model_tasks/` owns both layers of specialized task models:

1. **Bindings & discovery** (`model_tasks.py` main file plus constants/local targets/options) - normalized settings, target ID parsing, credential-gated discovery, local target descriptors, backend-owned option schemas for the Settings UI.
2. **Execution** - per-task services and their wire clients for speech, image, embeddings, video, music, decisions, and Live voice calls. Bindings and execution change together when a task type is added - one module on purpose. Shared internals: `TaskBindingResolver` (binding lookup + options merge + target parse) and central artifact handling; image/video/music write to caller-owned directories selected by their Tools.

`options.py` owns option validation and task dispatch. Its internal `_option_types.py` holds field records and shared constructors; `_image_options.py` and `_media_options.py` build image and speech/video/music fields from existing Model facts. Public option imports and schema behavior remain unchanged.

The package `__init__.py` resolves public exports on demand. The standalone managed STT worker imports `speech_local.py` without loading unrelated task implementations and their dependencies. Settings modules import Task Model constants from `constants.py` at module load; `paths.py` resolves target equality when preparing a patch, after package initialization.

Execution details live in child maps (`model_tasks/speech.md`, `image.md`, `embeddings.md`, `video.md`, `music.md`); the shared wire base class is `ProviderTaskClient` in `core/providers/task_client.py` (see `providers.md`). Runtime wires `TaskModelService` after providers/models/credentials/storage, then constructs the per-task execution services. Provider-backed target visibility delegates to `ModelRegistry.query()` plus usable credentials; local targets bypass catalogs/credentials but must register explicitly with `LocalTaskTargetRegistry`.

## Data Model

Supported task types (`constants.SUPPORTED_TASK_TYPES`): `speech_to_text`, `text_to_speech`, `image_understanding` (the text-output-from-image task behind route-gated `analyze_image`), `image_generation`, `video_generation`, `music_generation`, `text_embedding`, `decision`, `live_voice` (realtime voice calls; server-owned call lifecycle in `model_tasks/live.md`).

Bindings persist under `model_tasks` keyed by task type: non-empty `target` + options object. Public updates are sparse (options-only updates keep the existing target; empty target removes; Storage drops the section when empty); validation runs on each changed complete resulting binding before persistence; unchanged bindings are no-ops even if live catalog changes have made their options stale, and a changed target starts options at `{}` rather than inheriting incompatible ones. Storage's section merge follows the same target-change rule, so `settings.update` persists the binding that Task Model validation checked.

Provider target IDs use `<provider>/<model>::<connection-local-id>[:<account-id>]` (parser also accepts a provider-prefixed connection suffix; a trailing account pins credential selection while listing stays connection-level). Catalog IDs use the local connection id; persistence preserves accepted spelling. `task_model_targets_equal` compares parsed identity for Task Model, section and path updates, so an equivalent Connection spelling preserves options and unchanged stale bindings remain no-ops. Local IDs are `local/<id>` without `/` or `::`; descriptors reject unknown task types.

## Contracts

- `binding_for(task_type)` returns the configured binding or raises on unsupported/unconfigured.
- `validate_execution_target(binding)` owns the live target/Connection/Account gate for the exact binding snapshot an execution uses. `TaskBindingResolver.resolve` invokes it before options and client construction; save-time validation and a previous availability check never authorize later execution. Local executors keep ownership of live engine availability/error details.
- `binding_is_usable(task_type)` is the live preflight: rejects missing/stale bindings, incompatible capabilities, forbidden connections, unusable credentials, and unrunnable local targets. Most tasks check explicit `task_types`; `image_understanding` checks its actual modality contract (`text`+`image` input, text output) because provider catalogs may advertise modalities but omit derived tags. Execution services may deepen it with wire facts (e.g. ImageService gates Chat's `analyze_image`).
- Target listing sorts by kind/label/id and skips models whose connection allowlist forbids a usable connection - restricted models never cross-product against other connections of the same provider. Public descriptors expose ids/facts only; accessors never reparse labels into connection ids.
- Option schemas resolve model-aware (`option_schema_for(..., *, model, models, connection_id)`) falling back to conservative provider defaults without a registry hit; local targets surface descriptor-declared fields. `options_with_defaults` merges backend schema defaults under stored options before provider routing. A select option without available choices (for example no backend Model on the Connection) rejects every value.
- `patch_options` can unset a stored option even after it disappears from the live schema; the resulting binding must still validate. Unknown unset names that were never stored still fail validation.
- Server RPCs expose settings/update/patch_options/list_targets/status/options as thin delegates; validation errors, including unknown local targets and overflowing numeric options, map to stable `invalid_request`. Local target lookup errors also enter the task configuration-error boundary during execution. `task_model.status` is the Desktop Voice preflight.

Artifact identity is owned by `artifacts.py` and the image writer: `img_`, `aud_`, `vid_`, and `mus_` plus 12 lowercase base32 characters. Workspace files are claimed exclusively through `core/utils/ids.py`; sidecar-backed artifacts reserve the shared metadata basename before writing so collisions across extensions cannot replace metadata. Readers accept bounded safe opaque ids, preserving stored references. Collision and round-trip coverage: `tests/core/model_tasks/test_artifacts.py`, `test_image.py`.

Runtime injects the canonical `UsageRecorder` into every task execution service. `TaskUsage` in `task_execution.py` owns task identity and projection of existing response Usage: each task-client POST attempt, local speech execution, image-understanding Adapter send, Live voice call, and delegated Live backend send records consumption independently of retained artifacts, experiment history, or Sessions. Reported counters and costs survive unusable results and subsequent artifact failures; missing telemetry remains unknown. `TaskUsageContext` carries available Agent, Project, Session, Run, Extension and group identity from Tool callers without entering Model inputs. Standalone work, including Recall embedding batches, remains globally attributable to its Model without an invented Session. The durable store and Statistics projection are owned by `usage.md` and `statistics.md`.

## Conventions

- Option schemas are backend-owned render hints over Model-DB facts, never a hardcoded capability matrix. Accessors render field types generically (`text`, `textarea`, `select`, `number`, `boolean`, `json`) without provider-specific rules.
- A select may carry `options_by: {"field": <other field>, "values": {<other value>: [<choice>, ...]}}` (`TaskModelOptionsBy`; construction rejects a non-select, a self-reference, or unknown choices). Accessors show only the choices listed for the referenced field's current value (stored, else default), or all choices when that value has no entry; an entry with an empty list hides the whole field (that value makes it irrelevant; the stored value is kept and the runtime ignores it); when the referenced field changes and hides the current value, they switch to the field default if shown, else `""` if shown, else the first shown choice. It narrows display only: validation accepts every choice. First user: Live voice backend reasoning (`model_tasks/live.md`).
- Changed binding options validate against the resolved target schema before persistence - including generic Settings surfaces; required fields may satisfy via schema defaults.
- Per-model option **facts** live in `capabilities.task_options` (see `models.md`); this domain owns presentation only: enum -> select with leading Provider-default choice (forced-default exceptions get real values), range -> bounded number (collapsed/single-value enums skipped), boolean -> toggle or free-value number, string -> text, `model` -> select of tool-capable chat Models on the same Provider and Connection (`_live_options.py`). A spec `default` among the offered choices becomes the schema default. Runtime parameters and redundant shorthand fields never render. Models without facts fall back to conservative provider-level schemas.
- Two pseudo-options: `provider_options` (passthrough rendered only when advertised, sent nested as `provider.options`) and media-task `extra_options` escape hatch (not on `live_voice`) (adds non-empty provider-specific fields only; collisions with task-authored request fields fail locally as non-retryable Provider errors before send - helpers shared via the task client).
- Wire shaping belongs to per-task wire clients; add a new workflow in order: confirm the task type constant, ensure discovery produces matching capability tags, add option fields only if the UI needs them, implement service + wire client pair, add a child map.

## Constraints & Gotchas

- Task request accounting observes the execution boundaries above. Adapter-internal HTTP/OAuth/protocol repairs are not separately exposed as task attempts. Unknown media units are not converted into token counts or guessed costs, and past Task Model consumption without retained evidence cannot be reconstructed. Regression coverage: `tests/core/model_tasks/test_task_usage.py`, `test_live.py`, `test__live_brain.py`, `test_image_analysis.py`, `test_image_codex_provider.py`.
- The binding/discovery layer never calls media APIs or shapes wires; execution modules resolve bindings only through `TaskModelService`, never reading `settings.json`.
- Missing targets usually mean missing credentials or stale catalogs - refresh the Model DB after configuring keys instead of hand-editing generated files.
- Video/Music currently require OpenRouter (details in their child maps). Runtime registers STT `local/qwen3-asr` / `local/parakeet` and TTS `local/qwen3-tts` / `local/chatterbox` from the optional speech executor's catalog. Descriptors require a live availability callback for `usable`; registration alone does not imply an executable target. Imports and preflight never load ML runtimes or weights. Covered by `test_model_tasks.py` and `test_speech_local.py`.
- In packaged releases, optional local speech dependencies belong to managed data-directory environments and speech child processes, never the immutable release runtime. The source-install setup path remains a legacy separate behavior; see `model_tasks/speech.md`.
- `audio_generation` is a capability, not a configurable binding - generic audio output must not route as TTS or Music.
- Loaded `task_options` freeze into read-only views/tuples; schema builders accept both sequence forms.

## References

- Live voice calls: the `live_voice` binding, provider live wires, delegated reasoning, the call registry and owner socket, spoken app operations, or proactive Run announcements -> `model_tasks/live.md`

- Structured judgments, the evaluate Tool, Jev experiments, external application Actions and control lifecycle -> `model_tasks/decisions.md`
