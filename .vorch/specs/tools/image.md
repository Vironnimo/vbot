# Image Tool

Built-in `image_generation` tool for creating image artifacts through the
central image-generation task-model binding.

## Contract

Tool name: `image_generation`

Description: generates images from a text prompt using the configured image
generation model.

Provider-visible schema:

```json
{
  "type": "object",
  "properties": {
    "prompt": {
      "type": "string",
      "minLength": 1,
      "description": "The text prompt describing the image to generate."
    }
  },
  "required": ["prompt"],
  "additionalProperties": false
}
```

The tool intentionally exposes only `prompt`. Model, provider, size, aspect
ratio, and other image options come from Settings `model_tasks.image_generation`.

## Results

Success returns a normal tool success envelope:

```json
{
  "ok": true,
  "error": null,
  "data": {
    "message": "Image generation complete.",
    "images": [
      {
        "id": "a1b2c3d4...",
        "kind": "image",
        "filename": "a1b2c3d4....png",
        "media_type": "image/png",
        "size_bytes": 123456,
        "url": "/api/images/artifacts/a1b2c3d4...",
        "index": 0
      }
    ]
  },
  "artifacts": [
    {
      "id": "a1b2c3d4...",
      "kind": "image",
      "filename": "a1b2c3d4....png",
      "media_type": "image/png",
      "size_bytes": 123456,
      "url": "/api/images/artifacts/a1b2c3d4...",
      "index": 0
    }
  ]
}
```

Invalid or empty `prompt` returns `invalid_arguments`. Expected image failures
return `image_error` in the tool failure envelope instead of crashing the Run.

## Runtime

Runtime registers the tool at startup with the runtime-owned `ImageService`.
The tool uses `ImageService.generate_artifacts()` and never calls providers
directly.

## Constraints & Gotchas

- Do not add provider/model/image-option fields to the tool schema.
- The tool should remain a normal user-visible tool, not an internal tool.
- The returned artifact URL is a server-local image artifact URL, not an
  attachment URL.
