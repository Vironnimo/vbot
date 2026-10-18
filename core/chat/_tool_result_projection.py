"""Tool Result envelopes, media descriptors and public display snapshots."""

from __future__ import annotations

import json
from typing import Any

from core.chat.messages import JsonObject
from core.tools import (
    READ_MEDIA_ARTIFACT_KIND,
    InvalidToolResultError,
    ToolContext,
    ToolContract,
    ToolNotFoundError,
    is_tool_result_envelope,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("chat")


def _read_media_outputs(
    result: JsonObject,
    *,
    tool_call_id: str,
    tool_message_id: str,
) -> list[JsonObject]:
    """Extract request-local rich-content descriptors from a Tool Result.

    ``web_fetch`` and other stored image results carry compact attachment
    references. Chat resolves them into media content on the correlated Tool
    Result for the active Run. Other artifact kinds yield nothing. Correlation
    uses the Tool message identity: Providers may reuse Tool-call ids per turn.
    """
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return []

    outputs: list[JsonObject] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") != READ_MEDIA_ARTIFACT_KIND:
            continue
        attachment_id = artifact.get("attachment_id")
        filename = artifact.get("filename")
        media_type = artifact.get("media_type")
        if (
            isinstance(attachment_id, str)
            and isinstance(filename, str)
            and isinstance(media_type, str)
        ):
            outputs.append(
                {
                    "tool_call_id": tool_call_id,
                    "tool_message_id": tool_message_id,
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "media_type": media_type,
                }
            )
    return outputs


def _tool_display_payload(
    registry: Any,
    tool_name: str,
    arguments: Any,
    *,
    context: ToolContext | None = None,
    result: JsonObject | None = None,
) -> JsonObject:
    display_for_call = getattr(registry, "display_for_call", None)
    if not callable(display_for_call):
        return _empty_tool_display_payload()

    try:
        payload = display_for_call(tool_name, arguments, context=context, result=result)
    except ToolNotFoundError, TypeError, ValueError:
        return _empty_tool_display_payload()

    if not isinstance(payload, dict):
        return _empty_tool_display_payload()
    return payload


def _empty_tool_display_payload() -> JsonObject:
    return {
        "version": 1,
        "summary": "",
        "hidden_argument_keys": [],
        "primary": [],
        "facts": [],
    }


def _validated_tool_result(tool_name: str, result: Any) -> JsonObject:
    if not isinstance(result, dict):
        raise InvalidToolResultError(f"Tool handler must return a JSON object: {tool_name}")
    if not is_tool_result_envelope(result):
        raise InvalidToolResultError(
            f"Tool handler must return a valid result envelope: {tool_name}"
        )
    return result


def _validated_extension_tool_hook_result(
    *,
    registry: Any,
    tool_name: str,
    extension_name: str,
    hook_name: str,
    result: Any,
    contract: ToolContract | None = None,
) -> JsonObject | None:
    try:
        validator = getattr(registry, "validate_result", None)
        validated = (
            validator(tool_name, result, **({"contract": contract} if contract is not None else {}))
            if callable(validator)
            else _validated_tool_result(tool_name, result)
        )
        json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
        return validated
    except (TypeError, ValueError) as error:
        _LOGGER.warning(
            "Extension %r %s handler returned invalid tool result for %r: %s",
            extension_name,
            hook_name,
            tool_name,
            error,
        )
        return None
