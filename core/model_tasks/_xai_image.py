"""xAI Images generation and JSON edits, with inline inputs and outputs."""

from __future__ import annotations

import base64

from core.model_tasks.image_types import ImageInput, JsonObject
from core.providers.task_client import is_omittable_option, merge_extra_options


def xai_image_request(
    model: str, prompt: str, options: JsonObject, input_images: tuple[ImageInput, ...]
) -> tuple[str, JsonObject]:
    body: JsonObject = {"model": model, "prompt": prompt, "response_format": "b64_json"}
    for name in ("aspect_ratio", "resolution", "quality", "n"):
        if not is_omittable_option(options.get(name)):
            body[name] = options[name]
    if input_images:
        parts = [
            {
                "type": "image_url",
                "url": (
                    f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"
                ),
            }
            for image in input_images
        ]
        if len(parts) == 1:
            body["image"] = parts[0]
        else:
            body["images"] = parts
    merge_extra_options(body, options)
    return ("/images/edits" if input_images else "/images/generations"), body
