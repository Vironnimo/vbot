# Image Tool

Built-in `image_generation` tool for creating image artifacts through the central image-generation task-model binding.

## Interfaces

- Tool name: `image_generation`
- Registration: `register_image_generation_tool(registry, image_service)`
- Schema: required `prompt` (string, `minLength: 1`); `additionalProperties: false`. The tool intentionally exposes only `prompt` — model, provider, size, aspect ratio, and other options come from Settings `model_tasks.image_generation`.
- Display: summary field `prompt`.
- Success data: `{ message, images: [artifact, ...] }`. The model-facing `images` entries carry the artifact dict **plus** `path` (the absolute file path on disk) so the agent can copy/send/edit the file outside the chat; the top-level `artifacts` list stays path-free — the WebUI renders from `url`. `message` is an agent-facing instruction: the chat never renders image artifacts on its own, so the message includes ready-to-paste Markdown (`![description](url)`) and points at `path` for file operations.
- Artifact shape: `{ id, kind: "image", filename, media_type, size_bytes, url, index }`. `url` is a server-local image artifact URL (`/api/images/artifacts/<id>`), not an attachment URL.
- Invalid or empty `prompt` returns `invalid_arguments`. Expected image failures return `image_error` instead of crashing the Run.

## Runtime

Runtime registers the tool at startup with the runtime-owned `ImageService`. The tool uses `ImageService.generate_artifacts()` and never calls providers directly.

## Constraints & Gotchas

- Do not add provider/model/image-option fields to the tool schema.
- The tool should remain a normal user-visible tool, not an internal tool.
