"""Attachment and Tool-media preparation for canonical and live Chat requests."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from core.attachments.images import ImageConverter
from core.chat._request_history import (
    _request_content_resolution_inputs,
    _restore_in_run_tool_result_content,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import ContentBlock, MediaBlock, content_block_to_dict
from core.chat.messages import ChatMessage, JsonObject
from core.chat.wire_shaping import (
    PINNED_IMAGE_DELIVERY_SLOT,
    PINNED_IMAGE_RETIREMENT_SLOT,
    RequestImageBudget,
    limit_request_images,
)
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD

if TYPE_CHECKING:
    from core.chat._run_state import RequestBuildInputs
    from core.sessions import ChatSession, ChatSessionManager


class RequestMedia:
    """Resolve and retain request media without changing canonical message identities."""

    def __init__(
        self, attachment_resolver: ContentBlockResolver | None, sessions: ChatSessionManager
    ) -> None:
        self._attachment_resolver = attachment_resolver
        self._sessions = sessions
        self._tool_image_converter = ImageConverter()

    async def resolve_history(
        self,
        request_messages: list[JsonObject],
        effective_messages: list[ChatMessage],
        session: ChatSession,
        inputs: RequestBuildInputs,
        input_modalities: frozenset[str],
    ) -> list[JsonObject]:
        """Resolve the active user/Tool media and apply the Session's image-retirement pin."""
        if self._attachment_resolver is None:
            return request_messages
        current_user_message, read_media_outputs = await _CHAT_TRANSFORM_WORKERS.run(
            _request_content_resolution_inputs,
            effective_messages,
        )
        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all user content blocks resolve as historical.
        if current_user_message is not None:
            request_messages = await self._attachment_resolver.resolve_messages(
                request_messages,
                input_modalities=input_modalities,
                wire_media_types=inputs.wire_media_types,
                max_image_bytes=inputs.max_image_bytes,
            )

        await self.attach_tool_result_content(
            [message for message in request_messages if message.get("role") == "tool"],
            read_media_outputs,
            input_modalities,
            inputs.wire_media_types,
            max_image_bytes=inputs.max_image_bytes,
        )
        image_budget = inputs.image_budget or RequestImageBudget()
        if not image_budget.restored:
            retirement_pin = await _CHAT_TRANSFORM_WORKERS.run(
                self._sessions.prompt_pin,
                session.address,
                PINNED_IMAGE_RETIREMENT_SLOT,
            )
            delivery_pin = await _CHAT_TRANSFORM_WORKERS.run(
                self._sessions.prompt_pin,
                session.address,
                PINNED_IMAGE_DELIVERY_SLOT,
            )
            await _CHAT_TRANSFORM_WORKERS.run(image_budget.restore, retirement_pin, delivery_pin)
        return await _CHAT_TRANSFORM_WORKERS.run(
            limit_request_images,
            request_messages,
            budget=image_budget,
            image_limit=inputs.max_request_images,
            remember=inputs.fresh_prompt_epoch,
        )

    async def restore_tool_results(
        self,
        messages: list[JsonObject],
        live_messages: list[JsonObject],
        inputs: RequestBuildInputs,
    ) -> list[JsonObject]:
        """Restore current-Run Tool media that cannot be recovered from persisted history."""
        return await _restore_in_run_tool_result_content(
            messages,
            live_messages,
            input_modalities=inputs.input_modalities,
            wire_media_types=inputs.wire_media_types,
            image_budget=inputs.image_budget,
            image_limit=inputs.max_request_images,
            remember_image_retirement=inputs.fresh_prompt_epoch,
            image_converter=self._tool_image_converter,
            max_image_bytes=inputs.max_image_bytes,
        )

    async def transcribe_user_audio(
        self, content: str | list[ContentBlock]
    ) -> str | list[ContentBlock]:
        """Give incoming audio its speech-to-text before the user message persists."""
        if self._attachment_resolver is None:
            return content
        return await self._attachment_resolver.transcribe_audio(content)

    async def store_tool_media(
        self, tool_messages: list[ChatMessage], media_outputs: list[JsonObject]
    ) -> tuple[list[ChatMessage], list[JsonObject]]:
        """Store a Tool batch's loaded images and reference them from their Results.

        Stored images reach every later request of the Session from their Tool
        message, so a new Run sends the same bytes again and keeps the prompt cache.
        """
        if self._attachment_resolver is None or not any("base64" in m for m in media_outputs):
            return tool_messages, media_outputs
        loaded = ["base64" in media for media in media_outputs]
        media_outputs = await self._attachment_resolver.store_tool_images(media_outputs)
        stored: dict[str, list[JsonObject]] = {}
        for was_loaded, media in zip(loaded, media_outputs, strict=True):
            message_id = media.get("tool_message_id")
            if was_loaded and "base64" not in media and isinstance(message_id, str):
                stored.setdefault(message_id, []).append(
                    {key: media[key] for key in ("attachment_id", "filename", "media_type")}
                )
        return [
            replace(message, tool_media=[*(message.tool_media or []), *stored[message.id]])
            if message.id in stored
            else message
            for message in tool_messages
        ], media_outputs

    async def attach_tool_result_content(
        self,
        tool_messages: list[JsonObject],
        media_outputs: list[JsonObject],
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
        *,
        max_image_bytes: int | None = None,
    ) -> None:
        """Attach resolved media blocks to their correlated Tool Results.

        The persisted Tool message keeps only its compact result envelope. Base64
        blocks live exclusively in the in-flight request, so reading an image does
        not fabricate or persist a user turn. Correlation uses the Tool message
        identity because Providers may reuse Tool-call ids across turns.
        """

        if not media_outputs:
            return

        by_tool_message_id: dict[str, list[JsonObject]] = {}
        for media_output in media_outputs:
            tool_message_id = media_output.get("tool_message_id")
            if isinstance(tool_message_id, str):
                by_tool_message_id.setdefault(tool_message_id, []).append(media_output)

        for tool_message in tool_messages:
            tool_message_id = tool_message.get("id")
            matching = (
                by_tool_message_id.get(tool_message_id, [])
                if isinstance(tool_message_id, str)
                else []
            )
            if not matching:
                continue
            local_content = [
                block
                for media_output in matching
                if "base64" in media_output
                for block in await ContentBlockResolver.resolve_tool_image(
                    media_output,
                    input_modalities,
                    wire_media_types,
                    self._tool_image_converter,
                    max_image_bytes=max_image_bytes,
                )
            ]
            if local_content:
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = local_content
            matching = [media for media in matching if "attachment_id" in media]
            if not matching or self._attachment_resolver is None:
                continue
            content_blocks = [
                content_block_to_dict(
                    MediaBlock(
                        type="media",
                        attachment_id=media_output["attachment_id"],
                        filename=media_output["filename"],
                        media_type=media_output["media_type"],
                    )
                )
                for media_output in matching
            ]
            transient_message_id = f"tool-result:{tool_message_id}"
            resolved = await self._attachment_resolver.resolve_messages(
                [
                    {
                        "id": transient_message_id,
                        "role": "user",
                        "content": content_blocks,
                    }
                ],
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
                max_image_bytes=max_image_bytes,
            )
            resolved_content = resolved[0].get("content")
            if isinstance(resolved_content, list):
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = local_content + resolved_content
