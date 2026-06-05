# Image

Provider-neutral image generation execution, backed by the central task-model bindings.

## Overview

`core/image/` generates images from text prompts. It resolves the configured `image_generation` binding through `TaskModelService`, merges stored options with backend schema defaults, parses the target, and then routes to a provider-backed HTTP client.

The first implementation supports OpenRouter's `chat/completions` endpoint with `modalities: ["image", "text"]` and `image_config`. Local targets raise `ImageUnsupportedTargetError`. Non-OpenRouter providers raise `ProviderError(retryable=False)`.

## Interfaces

- `ImageService.generate(prompt) -> ImageGenerationResult`
- `ImageService.generate_artifacts(prompt) -> tuple[ImageArtifact, ...]`
- `ImageService.get_artifact(artifact_id) -> ImageArtifact`
- `ProviderImageClient.generate(prompt, options) -> ImageGenerationResult`

`ImageGenerationResult` contains raw image bytes, media type, model id, optional usage, and the raw response payload when available.

`ImageArtifact.to_dict()` returns:

```json
{
  "id": "a1b2c3d4...",
  "kind": "image",
  "filename": "a1b2c3d4....png",
  "media_type": "image/png",
  "size_bytes": 123456,
  "url": "/api/images/artifacts/a1b2c3d4...",
  "index": 0
}
```

## Provider Wire Behavior

OpenRouter sends JSON to `/chat/completions`:

```json
{
  "model": "openai/dall-e-3",
  "messages": [{"role": "user", "content": "<prompt>"}],
  "modalities": ["image"],
  "image_config": {
    "aspect_ratio": "1:1",
    "image_size": "1K"
  }
}
```

Response images arrive as base64 data URLs in `choices[0].message.images[]`. Each entry is decoded from base64 and stored as raw bytes. The media type is detected from the data URL prefix (e.g. `data:image/png;base64,...`).

## Artifacts

Image generation tool output is stored under `<data_dir>/images/` as one image file and one JSON metadata sidecar per generated image:

```
<data_dir>/images/
├── a1b2c3d4....png      ← raw image bytes
├── a1b2c3d4....json      ← metadata: id, filename, media_type, size_bytes, index
├── e5f6a7b8....jpg
├── e5f6a7b8....json
```

## HTTP Serving

`GET /api/images/artifacts/{artifact_id}` serves persisted image files as `FileResponse`. The endpoint validates the artifact id format (32-char hex), reads the metadata JSON, and verifies the image file exists.

## Error Classes

| Class | Situation | HTTP Status |
|---|---|---|
| `ImageConfigurationError` | No binding configured, empty prompt, invalid artifact id | 409 |
| `ImageUnsupportedTargetError` | Local target in image generation binding | 422 |
| `ImageExecutionError` | Provider request failure (unexpected) | 502 |

All error classes inherit from `ImageError(VBotError)`.

Provider-level errors (auth, rate limit, network) are raised from `ProviderImageClient` as `ProviderError` subclasses and are NOT caught by `ImageService` (they bubble up as unexpected).

## Extension Points

To add a new image generation provider:
1. Add an `elif self._provider.id == "..."` branch in `ProviderImageClient.generate()`
2. Implement a private `_generate_<provider>()` method that returns `ImageGenerationResult`
3. Add provider-specific option fields in `core/model_tasks/options.py` `_image_generation_fields()`
