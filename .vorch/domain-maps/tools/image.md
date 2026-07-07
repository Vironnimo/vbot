# Image Tool

Built-in `image_generation` tool for creating image artifacts through the central image-generation task-model binding.

## Interfaces

- Tool name: `image_generation`
- Registration: `register_image_generation_tool(registry, image_service)`
- Schema: required `prompt` (string, `minLength: 1`) plus two optional per-call intent knobs — `aspect_ratio` and `resolution` (both string); `additionalProperties: false`. All other image options (model, provider, size, quality, background, …) stay in Settings `model_tasks.image_generation`. The handler coerces the two knobs via `optional_string` (blank = omitted) into a `call_options` dict passed to `ImageService.generate_artifacts`; blank/absent knobs produce an empty dict so the execution layer's no-options path runs unchanged. Each knob is routed value-aware at the execution layer: sent as a native provider parameter when the resolved model advertises it with the requested value, otherwise appended to the prompt as a best-effort hint. A supplied knob overrides the Settings binding default for that call (per-call > binding > provider default). Routing/precedence live in `core/model_tasks/image.py` (`split_image_call_options`), not in the tool — see `.vorch/domain-maps/model_tasks/image.md`.
- Display: summary fields `prompt`, `aspect_ratio`, `resolution` (the two knobs surface on the tool chip only when supplied).
- Success data: `{ message, images: [artifact, ...] }`. The model-facing `images` entries carry the artifact dict **plus** `path` (the absolute file path on disk) so the agent can copy/send/edit the file outside the chat; the top-level `artifacts` list stays path-free — the WebUI renders from `url`. `message` is an agent-facing instruction: the chat never renders image artifacts on its own, so the message includes ready-to-paste Markdown (`![description](url)`) and points at `path` for file operations.
- Artifact shape: `{ id, kind: "image", filename, media_type, size_bytes, url, index }`. `url` is a server-local image artifact URL (`/api/images/artifacts/<id>`), not an attachment URL.
- Invalid or empty `prompt` returns `invalid_arguments`. Expected image failures return `image_error` instead of crashing the Run.

## Runtime

Runtime registers the tool at startup with the runtime-owned `ImageService`. The tool uses `ImageService.generate_artifacts()` and never calls providers directly.

## Constraints & Gotchas

- Only the two curated per-call intent knobs (`aspect_ratio`, `resolution`) belong on the tool schema; every other image option (model, provider, size, quality, background, seed, output format, …) stays Settings-only. Adding a third knob is a tool-schema property plus one hint-label entry in `split_image_call_options` — no execution-layer rework.
- The tool should remain a normal user-visible tool, not an internal tool.
