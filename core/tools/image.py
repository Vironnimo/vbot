"""Built-in image generation tool."""

from __future__ import annotations

from typing import Any

from core.image import ImageError
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
    "Generate images from a text prompt using the configured image generation model."
)
IMAGE_GENERATION_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": "The text prompt describing the image to generate.",
        }
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


def _image_display_message(artifacts: list[JsonObject]) -> str:
    """Tell the agent how to surface generated images in the chat.

    The chat does not render image artifacts on its own; the agent shows an
    image by embedding its artifact ``url`` as Markdown in its reply.
    """

    markdown_snippets = "\n".join(
        f"![generated image]({artifact['url']})" for artifact in artifacts
    )
    return (
        "Image generation complete. The chat does not display the image "
        "automatically — to show it, embed each image in your reply as Markdown "
        "using its 'url' field, e.g. ![short description](url). Markdown for the "
        f"image(s) you just generated:\n{markdown_snippets}"
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

        artifact_payloads = [a.to_dict() for a in artifacts]
        return tool_success(
            {
                "message": _image_display_message(artifact_payloads),
                "images": artifact_payloads,
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
