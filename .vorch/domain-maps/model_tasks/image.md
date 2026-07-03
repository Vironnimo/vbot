# Image

Provider-neutral image generation execution and artifact storage for the configured `image_generation` task-model binding.

## Overview

`core/model_tasks/` (`image*.py`) owns text-prompt image generation after Settings has selected one concrete task-model target. It resolves the `image_generation` binding through `TaskModelService`, merges stored options over backend defaults, rejects local targets, and routes provider targets to `ProviderImageClient`. It does not own model discovery, model catalogs, settings validation, UI controls, or the `image_generation` tool schema; those live in `core/models/`, `core/model_tasks/`, `core/settings/`, `webui/`, and `core/tools/image.py`.

## Interfaces

- `ImageService(model_tasks, runtime, data_dir)` — runtime-owned service; stores artifacts under `<data_dir>/images/`.
- `await ImageService.generate(prompt: str) -> ImageGenerationResult` — trims and validates `prompt`, resolves the configured binding, executes the provider request, and returns normalized bytes without persisting them.
- `await ImageService.generate_artifacts(prompt: str) -> tuple[ImageArtifact, ...]` — calls `generate()`, writes each returned image plus JSON metadata sidecar, and returns persisted artifact metadata.
- `ImageService.get_artifact(artifact_id: str) -> ImageArtifact` — accepts only 32-character lowercase hex ids, reads the metadata sidecar, recomputes `file_path` from the stored filename, and verifies the blob exists.
- `await ProviderImageClient.generate(prompt: str, *, options: dict) -> ImageGenerationResult` — provider-bound HTTP entrypoint; supports OpenRouter (the dedicated image API, `POST /images`), OpenAI Platform (`/v1/images/generations`), and OpenAI subscription image generation through Codex Responses (`/codex/responses`).

`ImageGenerationResult` contains `images: tuple[bytes, ...]`, one `media_type` for the result set, `model`, optional provider `usage`, and optional raw response payload. `ImageArtifact.to_dict()` returns `{ id, kind: "image", filename, media_type, size_bytes, url, index }`, where `url` is `/api/images/artifacts/<id>` and is not an attachment URL.

## Provider Wire Behavior

`ProviderImageClient` subclasses `core.providers.task_client.ProviderTaskClient`, which owns the shared plumbing (constructor tuple, `from_runtime` target resolution, auth headers, POST/classify/parse cycle, retry policy — see `providers.md`); this module owns only the image payload shapes and response parsing.

OpenRouter uses the selected provider connection's base URL (or provider base URL), connection auth header, provider `extra_headers`, a 120-second HTTP timeout, and `retry_async()` around retryable provider/network errors. The request is `POST /images` (OpenRouter's dedicated unified image API — the legacy chat/completions `image_config` path is gone; OpenRouter adds new image models exclusively to the image API) with `model`, `prompt`, and the unified top-level parameters present in the merged options: `aspect_ratio`, `resolution`, `size`, `quality`, `output_format`, `background`, `output_compression`, `n`, `seed` (`_UNIFIED_IMAGE_KEYS`). Absent keys are never invented. Empty placeholder values (`None`, `""`, `[]`, `{}`) are treated as unset and dropped (the option schema's "Provider default" select value is `""`), while numeric `0`/`0.0` and `False` are real values and are kept — the shared rule lives in `core/providers/task_client.py` (`is_omittable_option`). The same empty-placeholder omission applies to the OpenAI `/v1/images/generations` keys.

Two schema-owned pseudo-options never go on the wire verbatim: `provider_options` (a JSON object keyed by upstream provider slug, e.g. `{"recraft": {"style": …}}`) becomes the nested `provider.options` request object — the passthrough channel for provider-specific keys the model's `task_options.passthrough` advertises; `extra_options` merges into the top-level payload last and overrides authored keys (the universal escape hatch, `merge_extra_options`).

The unified response is `{"created", "data": [{"b64_json", "media_type"?}], "usage"}`: every `b64_json` entry decodes into the `images` tuple, a per-entry `media_type` (vector outputs) wins, else the requested `output_format` decides, fallback `image/png`. Missing/empty `data` or undecodable entries are `ProviderError(retryable=True)` at the client layer and become `ImageExecutionError` when surfaced through `ImageService.generate()`.

OpenAI native image generation sends `POST /v1/images/generations` with `model`, `prompt`, and optional image-gen keys (`n`, `size`, `quality`, `background`, `moderation`, `output_format`, `output_compression`, `style`, `response_format`); `extra_options` merges last here too. The response `data[].b64_json` entries are decoded as Base64 images; `n>1` returns one `ImageGenerationResult` with multiple images. `url`-only responses are rejected. The response parser derives `media_type` from the requested `output_format` (since the response body does not echo it).

OpenAI subscription image generation is selected by provider `openai` plus connection mode `codex_responses` and posts to the connection base URL's `/codex/responses` endpoint, not to `/images/generations`. The request uses a named carrier model constant (`gpt-5.5`), `stream: true`, `store: false`, instructions `You are an image generation assistant.`, one user `input_text` asking the carrier to use the `image_generation` tool, and a single tool object `{"type": "image_generation", ...}`. Headers are rebuilt per retry from the refresh-capable task token getter and add `chatgpt-account-id` derived from the current OAuth JWT plus `CODEX_EXTRA_HEADERS`; a missing account id is an auth error asking the user to reconnect. The Codex tool whitelist from stored binding options is `output_format`, `output_compression`, `moderation`, `background`, `size`, and `quality`; `n` and `model` are always dropped (also from `extra_options`) because the wire rejects `n` and forces the backend image model. `extra_options` merges into the tool object last for future wire fields, while `size`, `quality`, and `background` are also woven into the input text because the backend treats the tool-level values as advisory. The Codex path uses a 300-second timeout and buffers the SSE response body; progressive `partial_image` frames are ignored, final images come from `response.output_item.done` items of type `image_generation_call`, usage is read from `response.completed.response.tool_usage.image_gen` and `response.completed.response.usage`, and `raw` keeps the final image call metadata such as `revised_prompt`, actual `size`, and actual `quality`.

## Artifacts

Image artifacts are stored through the shared `TaskArtifactStore` (`core/model_tasks/artifacts.py`) as one blob and one JSON sidecar per image:

```text
<data_dir>/images/
  a1b2c3d4....png
  a1b2c3d4....json
```

Artifact ids are `uuid4().hex`. The filename extension is inferred from the result media type (`png`, `jpg`, `webp`, `gif`, `bmp`, `svg`, fallback `png`), not from provider filenames. Sidecars store `id`, `filename`, `media_type`, `size_bytes`, and `index`; `file_path` is never trusted from metadata.

## HTTP Serving

`GET /api/images/artifacts/{artifact_id}` fetches the runtime `image` service and serves the artifact blob with `FileResponse(media_type=artifact.media_type, filename=artifact.filename)`. If the runtime has no image service, the endpoint returns 503. Expected image errors map to HTTP as: `ImageConfigurationError` -> 409, `ImageUnsupportedTargetError` -> 422, `ImageExecutionError` -> 502, other `ImageError` -> 400.

## Constraints & Gotchas

- Provider targets must use the task-model id shape `provider/model-id::connection-id`; nested model ids such as `openrouter/openai/gpt-image::api-key` are valid. Local targets parse successfully in `core/model_tasks/` but image execution rejects them with `ImageUnsupportedTargetError`.
- A configured provider target outside the implemented image providers reaches `ProviderImageClient.generate()`, raises `ProviderError(retryable=False)`, and is surfaced by `ImageService.generate()` as `ImageExecutionError`, not `ImageUnsupportedTargetError`.
- `image_generation` targets are discovered from model capabilities where `output_modalities` includes `image`; do not hard-code image models in `core/model_tasks/` (`image*.py`).
- Per-model image option **facts** (allowed values, ranges, passthrough keys) live in the model DB (`capabilities.task_options` — auto-projected from the OpenRouter image API at refresh, hand-authored in `resources/models/openai.overrides.json` for OpenAI native). Only render hints belong in `core/model_tasks/options.py`; the agent-facing `image_generation` tool intentionally accepts only `prompt`.
- A stored binding option the wire does not know (e.g. the legacy `image_size` key from before the unified-API migration) is silently dropped at request build — re-saving the binding options in Settings clears it.
- New provider execution belongs in `ProviderImageClient` and should keep returning normalized `ImageGenerationResult`; do not route image generation through chat adapters or attachment storage.
- Debug trace capture is not wired through `ProviderImageClient`; the shared `ProviderTaskClient.post_and_parse` constructs a plain `httpx.AsyncClient` rather than `core.providers._http_shared.build_async_client()` (deliberate).
- `ImageError` derives from the shared `TaskError` base in `core/utils/errors.py`; the HTTP mappings above are unchanged by that.
- `generate_artifacts()` (via the shared `TaskArtifactStore`) writes blob then sidecar without a rollback transaction. Treat partially written artifacts as possible if the process dies mid-write; `get_artifact()` already fails closed when metadata or blob is missing/unreadable.
