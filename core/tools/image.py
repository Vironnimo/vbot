"""Built-in image generation tool."""

from __future__ import annotations

from typing import Any

from core.model_tasks import ImageError
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

IMAGE_GENERATION_TOOL_NAME = "image_generation"
IMAGE_GENERATION_TOOL_DESCRIPTION = (
    "Generate images from a text prompt using the configured image generation "
    "model. The result includes each image's file path and the Markdown that "
    "displays it in the chat."
)
IMAGE_GENERATION_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The text prompt describing the image to generate. Be specific "
                "and concrete — name the subject and its key attributes, the "
                "setting, composition/framing, lighting, mood, and the visual "
                "medium or style (e.g. photo, oil painting, 3D render, flat "
                "vector). Detailed prompts produce markedly better images than "
                "short vague ones; spell the scene out fully rather than "
                "referring to things from the conversation."
            ),
        }
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


def _image_display_message(artifacts: list[JsonObject]) -> str:
    """Tell the agent how to surface generated images in the chat.

    The chat renders an image only when the agent embeds its artifact ``url``
    as Markdown in the reply; ``path`` is the file on disk for everything else.
    """

    markdown_snippets = "\n".join(
        f"![generated image]({artifact['url']})" for artifact in artifacts
    )
    return (
        "Image generation complete. The chat displays an image only when you "
        f"embed its Markdown in your reply:\n{markdown_snippets}\n"
        "For file operations (copy, send, edit) use its 'path'."
    )


def make_image_generation_handler(image_service: Any):
    """Create an image generation tool handler bound to the runtime image service."""

    async def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return tool_failure("invalid_arguments", "prompt must be a non-empty string")

        try:
            artifacts = await image_service.generate_artifacts(prompt)
        except ImageError as exc:
            return tool_failure("image_error", str(exc))

        # The model-facing copies carry each image file's absolute path so the
        # agent can use the file outside the chat; the UI-facing artifacts
        # payload stays path-free — the WebUI renders from `url`.
        artifact_payloads = [a.to_dict() for a in artifacts]
        image_payloads = [
            {**payload, "path": str(artifact.file_path)}
            for artifact, payload in zip(artifacts, artifact_payloads, strict=True)
        ]
        return tool_success(
            {
                "message": _image_display_message(image_payloads),
                "images": image_payloads,
            },
            artifacts=artifact_payloads,
        )

    return handler


def register_image_generation_tool(registry: ToolRegistry, image_service: Any) -> None:
    """Register the image generation tool with a vBot tool registry."""

    registry.register(
        IMAGE_GENERATION_TOOL_NAME,
        IMAGE_GENERATION_TOOL_DESCRIPTION,
        IMAGE_GENERATION_TOOL_PARAMETERS,
        make_image_generation_handler(image_service),
        display=ToolDisplay(summary_fields=("prompt",)),
    )
