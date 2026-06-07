# Image

Provider-neutral image generation execution and artifact storage for the configured `image_generation` task-model binding.

## Overview

`core/image/` owns text-prompt image generation after Settings has selected one concrete task-model target. It resolves the `image_generation` binding through `TaskModelService`, merges stored options over backend defaults, rejects local targets, and routes provider targets to `ProviderImageClient`. It does not own model discovery, model catalogs, settings validation, UI controls, or the `image_generation` tool schema; those live in `core/models/`, `core/model_tasks/`, `core/settings/`, `webui/`, and `core/tools/image.py`.

## Interfaces

- `ImageService(model_tasks, runtime, data_dir)` — runtime-owned service; stores artifacts under `<data_dir>/images/`.
- `await ImageService.generate(prompt: str) -> ImageGenerationResult` — trims and validates `prompt`, resolves the configured binding, executes the provider request, and returns normalized bytes without persisting them.
- `await ImageService.generate_artifacts(prompt: str) -> tuple[ImageArtifact, ...]` — calls `generate()`, writes each returned image plus JSON metadata sidecar, and returns persisted artifact metadata.
- `ImageService.get_artifact(artifact_id: str) -> ImageArtifact` — accepts only 32-character lowercase hex ids, reads the metadata sidecar, recomputes `file_path` from the stored filename, and verifies the blob exists.
- `await ProviderImageClient.generate(prompt: str, *, options: dict) -> ImageGenerationResult` — provider-bound HTTP entrypoint; supports OpenRouter (`/chat/completions`) and OpenAI (`/v1/images/generations`).

`ImageGenerationResult` contains `images: tuple[bytes, ...]`, one `media_type` for the result set, `model`, optional provider `usage`, and optional raw response payload. `ImageArtifact.to_dict()` returns `{ id, kind: "image", filename, media_type, size_bytes, url, index }`, where `url` is `/api/images/artifacts/<id>` and is not an attachment URL.

## Provider Wire Behavior

OpenRouter uses the selected provider connection's base URL (or provider base URL), connection auth header, provider `extra_headers`, a 120-second HTTP timeout, and `retry_async()` around retryable provider/network errors. The request is `POST /chat/completions` with `model`, one user text message, `modalities: ["image"]`, and `image_config` built from the task-model options. Only known `image_config` keys present in the options dict are forwarded — absent keys are never invented.

Universal image_config keys forwarded: `aspect_ratio`, `image_size`. Top-level `seed` is sent separately (not under `image_config`) when present in options and the model's `supported_parameters` includes `"seed"`.

Model-specific aspect-ratio overrides: `microsoft/mai-image-2.5` uses a reduced set; `google/gemini-3.1-flash-image-preview` uses an extended set (base + `1:4,4:1,1:8,8:1`) and adds `image_size` value `0.5K`.

Recraft family (`recraft/*`) keys forwarded under `image_config`: `strength` (v3/v4/v4.1), `style` and `text_layout` (v3 only), `rgb_colors` and `background_rgb_color` (v3/v4/v4.1/v4-pro).

Sourceful family (`sourceful/*`) keys forwarded under `image_config`: `font_inputs` (v2/v2.5), `super_resolution_references` (v2 only), `scoring_prompt`, `scoring_rubric`, `background_mode`, `background_hex_color` (v2.5 only).

Response images are read from `choices[0].message.images[]`. Entries may be `{ image_url: { url } }`, `{ url }`, or a raw string URL; only Base64 data URLs matching `data:<media-type>;base64,<payload>` are decoded. Missing choices/messages/images or undecodable image payloads are `ProviderError(retryable=True)` at the client layer and become `ImageExecutionError` when surfaced through `ImageService.generate()`.

OpenAI native image generation sends `POST /v1/images/generations` with `model`, `prompt`, and optional image-gen keys (`size`, `quality`, `background`, `n`, `output_format`, `style`, `response_format`). The response `data[].b64_json` entries are decoded as Base64 images; `n>1` returns one `ImageGenerationResult` with multiple images. `url`-only responses are rejected. The response parser derives `media_type` from the requested `output_format` (since the response body does not echo it).

## Artifacts

Image artifacts are stored as one blob and one JSON sidecar per image:

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
- A configured non-OpenRouter provider target reaches `ProviderImageClient.generate()`, raises `ProviderError(retryable=False)`, and is surfaced by `ImageService.generate()` as `ImageExecutionError`, not `ImageUnsupportedTargetError`.
- `image_generation` targets are discovered from model capabilities where `output_modalities` includes `image`; do not hard-code image models in `core/image/`.
- Provider/image options belong in `core/model_tasks/options.py`; the agent-facing `image_generation` tool intentionally accepts only `prompt`.
- New provider execution belongs in `ProviderImageClient` and should keep returning normalized `ImageGenerationResult`; do not route image generation through chat adapters or attachment storage.
- Debug trace capture is not wired through `ProviderImageClient`; it constructs a plain `httpx.AsyncClient` rather than `core.providers._http_shared.build_async_client()`.
- `generate_artifacts()` writes blob then sidecar without a rollback transaction. Treat partially written artifacts as possible if the process dies mid-write; `get_artifact()` already fails closed when metadata or blob is missing/unreadable.
