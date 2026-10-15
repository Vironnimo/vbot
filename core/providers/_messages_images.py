"""Prepare image dimensions against the complete Messages request."""

from __future__ import annotations

import base64
import io
import warnings
from collections.abc import Iterator
from typing import Any

from PIL import Image

from core.attachments.images import ImageConversionError, ImageConverter
from core.providers.errors import ProviderError
from core.providers.wire_profile import MediaRules

_HEADER_BYTES = 256 * 1024


def prepare_message_images(messages: Any, converter: ImageConverter, rules: MediaRules) -> Any:
    """Return request-only copies; callers run this in the request-body worker."""
    # Body overrides are free-form wire values, not canonical messages. Prepare
    # recognizable content only; leave other shapes for the Provider to handle.
    if not isinstance(messages, list):
        return messages
    dimension = rules.image_max_dimension
    if (
        rules.many_images_threshold is not None
        and rules.many_images_max_dimension is not None
        and sum(1 for _ in _image_blocks(messages)) > rules.many_images_threshold
    ):
        dimension = (
            min(dimension, rules.many_images_max_dimension)
            if dimension
            else rules.many_images_max_dimension
        )
    if dimension is None:
        return messages

    def prepare_blocks(blocks: Any) -> Any:
        if not isinstance(blocks, list):
            return blocks
        prepared_blocks = []
        for block in blocks:
            if not isinstance(block, dict):
                prepared_blocks.append(block)
                continue
            if block.get("type") == "tool_result" and "content" in block:
                prepared_blocks.append({**block, "content": prepare_blocks(block.get("content"))})
                continue
            source = block.get("source")
            if (
                block.get("type") != "image"
                or not isinstance(source, dict)
                or source.get("type") != "base64"
            ):
                prepared_blocks.append(block)
                continue
            encoded = source.get("data")
            size = _dimensions(encoded)
            if not isinstance(encoded, str) or size is None or max(size) <= dimension:
                prepared_blocks.append(block)
                continue
            try:
                prepared = converter.prepare(
                    base64.b64decode(encoded, validate=True),
                    source.get("media_type", ""),
                    rules.types,
                    max_output_bytes=rules.image_max_bytes,
                    max_dimension=dimension,
                )
            except (ImageConversionError, ValueError) as exc:
                raise ProviderError(
                    f"Image cannot fit this request's limits: {exc}", retryable=False
                ) from exc
            prepared_blocks.append(
                {
                    **block,
                    "source": {
                        **source,
                        "media_type": prepared.media_type,
                        "data": base64.b64encode(prepared.data).decode("ascii"),
                    },
                }
            )
            if prepared.note:
                prepared_blocks.append(
                    {"type": "text", "text": f"[For the current Model, {prepared.note}.]"}
                )
        return prepared_blocks

    return [
        {**message, "content": prepare_blocks(message["content"])}
        if isinstance(message, dict) and "content" in message
        else message
        for message in messages
    ]


def _image_blocks(messages: list[Any]) -> Iterator[dict[str, Any]]:
    def blocks_in(content: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image":
                    yield block
                elif block.get("type") == "tool_result":
                    yield from blocks_in(block.get("content"))

    for message in messages:
        if isinstance(message, dict):
            yield from blocks_in(message.get("content"))


def _dimensions(encoded: Any) -> tuple[int, int] | None:
    """Inspect a small prefix first, extending it for images with large metadata."""
    if not isinstance(encoded, str):
        return None
    prefix = encoded[: (_HEADER_BYTES // 3) * 4]
    for candidate in (prefix, encoded) if len(prefix) < len(encoded) else (encoded,):
        try:
            header = base64.b64decode(candidate, validate=True)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(header)) as image:
                    return image.size
        except OSError, ValueError, SyntaxError, Image.DecompressionBombError:
            pass
    return None
