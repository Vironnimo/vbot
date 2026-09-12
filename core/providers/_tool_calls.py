"""Canonical Tool Call normalization, Result projection and wire identifier profiles."""

from __future__ import annotations

import copy
import hashlib
import json
import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]

_ALPHANUMERIC_TOOL_CALL_ID_CHARACTERS = frozenset(string.ascii_letters + string.digits)

_DASH_UNDERSCORE_TOOL_CALL_ID_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_-")

_TOOL_CALL_ID_HASH_LENGTH = 12

_RESPONSES_OUTPUT_META_KEY = "response_output"

_TOOL_RESULT_ENVELOPE_KEYS = frozenset({"ok", "error", "data", "artifacts"})

TOOL_RESULT_CONTENT_BLOCKS_FIELD = "tool_result_content"

TOOL_CALL_REJECTION_FIELD = "rejection"

TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD = "argument_sequence_index"

TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD = "argument_sequence_length"

INVALID_TOOL_CALL_NAME = "invalid_tool_call"

MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS = 1200


def normalize_tool_call_candidates(
    *,
    tool_call_id: Any,
    name: Any,
    arguments: Any,
    fallback_id: str,
    rejection: Any = None,
    argument_sequence_index: Any = None,
    argument_sequence_length: Any = None,
) -> list[JsonObject]:
    """Return one or more canonical calls from one Provider Tool attempt.

    Some Models encode sibling calls as consecutive top-level JSON values in one
    ``arguments`` string. A fully decodable sequence is unambiguous enough to
    preserve each value as an independently validated call. Calls reconstructed
    this way carry provenance metadata for persistence and Provider replay;
    scheduling remains identical to native sibling Tool Calls.
    """

    decoded_sequence = _decode_tool_argument_sequence(arguments)
    if decoded_sequence is None:
        return [
            normalize_tool_call_candidate(
                tool_call_id=tool_call_id,
                name=name,
                arguments=arguments,
                fallback_id=fallback_id,
                rejection=rejection,
                argument_sequence_index=argument_sequence_index,
                argument_sequence_length=argument_sequence_length,
            )
        ]

    resolved_id = tool_call_id if isinstance(tool_call_id, str) and tool_call_id else fallback_id
    sequence_length = len(decoded_sequence)
    candidates: list[JsonObject] = []
    for index, decoded_arguments in enumerate(decoded_sequence):
        candidate_id = (
            resolved_id if index == 0 else _recovered_tool_call_id(resolved_id, fallback_id, index)
        )
        candidate_arguments = (
            decoded_arguments
            if isinstance(decoded_arguments, Mapping)
            else json.dumps(decoded_arguments, ensure_ascii=False, separators=(",", ":"))
        )
        candidates.append(
            normalize_tool_call_candidate(
                tool_call_id=candidate_id,
                name=name,
                arguments=candidate_arguments,
                fallback_id=fallback_id,
                rejection=rejection if index == 0 else None,
                argument_sequence_index=index,
                argument_sequence_length=sequence_length,
            )
        )
    return candidates


def normalize_tool_call_candidate(
    *,
    tool_call_id: Any,
    name: Any,
    arguments: Any,
    fallback_id: str,
    rejection: Any = None,
    argument_sequence_index: Any = None,
    argument_sequence_length: Any = None,
) -> JsonObject:
    """Return one safe canonical Tool Call without discarding malformed attempts.

    Provider wires disagree on whether arguments arrive as an object or an encoded
    JSON string, and some omit call ids. Every recognizable call still needs a
    stable canonical identity so Chat can correlate a failure Result. Malformed
    names or arguments are therefore represented with safe placeholder fields and
    an explicit rejection; Chat will return that rejection without dispatching the
    Tool or its Extension hooks.
    """

    resolved_id = tool_call_id if isinstance(tool_call_id, str) and tool_call_id else fallback_id
    resolved_name = name if isinstance(name, str) and name else INVALID_TOOL_CALL_NAME
    normalized_arguments, argument_error = _normalize_tool_call_arguments(arguments)

    problems: list[str] = []
    if resolved_name == INVALID_TOOL_CALL_NAME and name != INVALID_TOOL_CALL_NAME:
        problems.append("the Tool name is missing or is not a non-empty string")
    if argument_error is not None:
        problems.append(argument_error)

    normalized_rejection = _normalized_existing_tool_call_rejection(rejection)
    if rejection is not None and normalized_rejection is None:
        problems.append("the Provider supplied invalid Tool Call rejection metadata")

    candidate: JsonObject = {
        "id": resolved_id,
        "name": resolved_name,
        "arguments": normalized_arguments,
    }
    if _valid_argument_sequence_metadata(
        argument_sequence_index,
        argument_sequence_length,
    ):
        candidate[TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD] = argument_sequence_index
        candidate[TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD] = argument_sequence_length
    if problems:
        code = (
            "malformed_tool_arguments"
            if len(problems) == 1 and argument_error
            else "malformed_tool_call"
        )
        detail = "; ".join(problems)
        candidate[TOOL_CALL_REJECTION_FIELD] = {
            "code": code,
            "message": (
                f"Tool Call {resolved_id!r} was rejected before execution because {detail}. "
                "Reissue the complete Tool Call with a non-empty Tool name and arguments "
                "encoded as one JSON object."
            ),
            "fingerprint": _tool_call_rejection_fingerprint(name, arguments, detail),
        }
    elif normalized_rejection is not None:
        candidate[TOOL_CALL_REJECTION_FIELD] = normalized_rejection
    return candidate


def _decode_tool_argument_sequence(arguments: Any) -> list[Any] | None:
    if not isinstance(arguments, str) or not arguments:
        return None

    decoder = json.JSONDecoder()
    values: list[Any] = []
    position = 0
    while position < len(arguments):
        while position < len(arguments) and arguments[position] in " \t\r\n":
            position += 1
        if position == len(arguments):
            break
        try:
            value, position = decoder.raw_decode(arguments, position)
        except json.JSONDecodeError:
            return None
        values.append(value)
    return values if len(values) > 1 else None


def _recovered_tool_call_id(base_id: str, fallback_id: str, index: int) -> str:
    evidence = f"{base_id}\0{fallback_id}\0{index}"
    digest = hashlib.sha256(evidence.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"tool_call_recovered_{digest}"


def _valid_argument_sequence_metadata(index: Any, length: Any) -> bool:
    return (
        isinstance(index, int)
        and not isinstance(index, bool)
        and isinstance(length, int)
        and not isinstance(length, bool)
        and length > 1
        and 0 <= index < length
    )


def _normalize_tool_call_arguments(arguments: Any) -> tuple[JsonObject, str | None]:
    if arguments is None or (isinstance(arguments, str) and not arguments):
        return {}, None
    if isinstance(arguments, Mapping):
        normalized = dict(arguments)
    elif isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            return {}, (
                "the arguments contain malformed or incomplete JSON "
                f"({len(arguments)} chars): {_preview_malformed_tool_arguments(arguments)}"
            )
        if not isinstance(decoded, Mapping):
            return {}, (
                "the arguments JSON decoded to "
                f"{type(decoded).__name__}, but a JSON object is required"
            )
        normalized = dict(decoded)
    else:
        return {}, (
            "the arguments must be a JSON object or an encoded JSON object, but received "
            f"{type(arguments).__name__}"
        )

    if any(not isinstance(key, str) for key in normalized):
        return {}, "the arguments object contains a non-string property name"
    try:
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as error:
        return {}, f"the arguments object is not JSON-serializable: {error}"
    return normalized, None


def _normalized_existing_tool_call_rejection(value: Any) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    code = value.get("code")
    message = value.get("message")
    fingerprint = value.get("fingerprint")
    if not all(isinstance(field, str) and field for field in (code, message, fingerprint)):
        return None
    return {"code": code, "message": message, "fingerprint": fingerprint}


def _preview_malformed_tool_arguments(arguments_text: str) -> str:
    if len(arguments_text) <= MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS:
        return repr(arguments_text)
    edge_length = MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS // 2
    head = arguments_text[:edge_length]
    tail = arguments_text[-edge_length:]
    omitted_count = len(arguments_text) - (edge_length * 2)
    return f"{head!r} ... <{omitted_count} chars omitted> ... {tail!r}"


def _tool_call_rejection_fingerprint(name: Any, arguments: Any, detail: str) -> str:
    if isinstance(arguments, str):
        argument_evidence = arguments
    else:
        try:
            argument_evidence = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda value: f"<{type(value).__name__}>",
            )
        except (TypeError, ValueError, OverflowError):
            argument_evidence = f"<{type(arguments).__name__}>"
    evidence = f"{name!r}\0{argument_evidence}\0{detail}"
    return hashlib.sha256(evidence.encode("utf-8", errors="replace")).hexdigest()


def canonical_tool_result_is_error(message: Mapping[str, Any]) -> bool:
    """Return whether a canonical Tool message carries a failure envelope.

    Provider Adapters use this request-only projection to add native error
    signals without changing the serialized Tool Result content or teaching
    Chat about Provider wire fields. Legacy/non-envelope Tool content remains
    ordinary content and therefore receives no native failure flag.
    """

    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        result = json.loads(content)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(result, Mapping)
        and frozenset(result) == _TOOL_RESULT_ENVELOPE_KEYS
        and result.get("ok") is False
        and result.get("data") is None
        and isinstance(result.get("error"), Mapping)
        and isinstance(result.get("artifacts"), list)
    )


def tool_result_content_blocks(message: Mapping[str, Any]) -> list[JsonObject]:
    """Return the Run-local rich content attached to one Tool Result.

    Persisted Tool messages keep their stable JSON-string envelope. Chat adds
    this request-only field after resolving attachment references, so Provider
    adapters can render native multimodal Tool Results without putting base64
    data into Session history.
    """

    value = message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD)
    if not isinstance(value, list):
        return []
    return [dict(block) for block in value if isinstance(block, Mapping)]


def project_tool_result_content_fallbacks(
    messages: list[JsonObject],
) -> list[JsonObject]:
    """Project rich Tool Results onto text-only Tool wires.

    Supplemental text remains part of its correlated Tool Result. Media blocks
    are emitted in one request-only user message after the complete consecutive
    Tool Result batch, preserving Provider tool-cycle ordering without writing
    a synthetic user message to the canonical Session.
    """

    projected: list[JsonObject] = []
    pending_media: list[JsonObject] = []

    def flush_media() -> None:
        if pending_media:
            projected.append({"role": "user", "content": list(pending_media)})
            pending_media.clear()

    for message in messages:
        if message.get("role") != "tool":
            flush_media()
            projected.append(dict(message))
            continue

        projected_message = dict(message)
        projected_message.pop(TOOL_RESULT_CONTENT_BLOCKS_FIELD, None)
        supplemental_text: list[str] = []
        for block in tool_result_content_blocks(message):
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    supplemental_text.append(text)
            elif block_type in {"media", "document"}:
                pending_media.append(block)
        if supplemental_text:
            content = projected_message.get("content")
            base_text = content if isinstance(content, str) else ""
            projected_message["content"] = "\n\n".join(
                part for part in (base_text, *supplemental_text) if part
            )
        projected.append(projected_message)

    flush_media()
    return projected


@dataclass(frozen=True)
class ToolCallIdProfile:
    """Verified target-wire constraints for request Tool-call identifiers."""

    name: str
    allowed_characters: frozenset[str]
    max_length: int
    exact_length: int | None = None
    trim_trailing_underscores: bool = False
    rewrite_responses_output_items: bool = False


ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE = ToolCallIdProfile(
    name="anthropic_messages",
    allowed_characters=_DASH_UNDERSCORE_TOOL_CALL_ID_CHARACTERS,
    max_length=64,
)

MISTRAL_TOOL_CALL_ID_PROFILE = ToolCallIdProfile(
    name="mistral",
    allowed_characters=_ALPHANUMERIC_TOOL_CALL_ID_CHARACTERS,
    max_length=9,
    exact_length=9,
)

RESPONSES_TOOL_CALL_ID_PROFILE = ToolCallIdProfile(
    name="responses",
    allowed_characters=_DASH_UNDERSCORE_TOOL_CALL_ID_CHARACTERS,
    max_length=64,
    trim_trailing_underscores=True,
    rewrite_responses_output_items=True,
)


def normalize_tool_call_ids(
    messages: list[JsonObject],
    profile: ToolCallIdProfile,
) -> list[JsonObject]:
    """Return a target-wire copy with paired Tool call/result IDs normalized.

    Canonical Session messages are Provider-origin evidence and must stay
    immutable. This transform therefore deep-copies the complete request view,
    allocates collision-free IDs across the outgoing request, and scopes result
    correlation to the immediately preceding Assistant Tool batch. Reusing one
    original ID in a later batch creates a fresh wire ID rather than aliasing
    two logical calls.
    """

    request_messages = copy.deepcopy(messages)
    reserved_valid_ids = _reserved_valid_tool_call_ids(request_messages, profile)
    used_wire_ids: set[str] = set()
    active_result_ids: dict[str, list[str]] = {}

    for message_index, message in enumerate(request_messages):
        role = message.get("role")
        if role == "assistant":
            active_result_ids = _normalize_assistant_tool_call_ids(
                message,
                profile,
                used_wire_ids=used_wire_ids,
                reserved_valid_ids=reserved_valid_ids,
                message_index=message_index,
            )
            continue
        if role == "tool":
            original_id = message.get("tool_call_id")
            if isinstance(original_id, str):
                mapped_ids = active_result_ids.get(original_id)
                if mapped_ids:
                    message["tool_call_id"] = mapped_ids.pop(0)
            continue
        active_result_ids = {}

    return request_messages


def _reserved_valid_tool_call_ids(
    messages: list[JsonObject],
    profile: ToolCallIdProfile,
) -> set[str]:
    reserved: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and _tool_call_id_is_valid(tool_call_id, profile):
                reserved.add(tool_call_id)
    return reserved


def _normalize_assistant_tool_call_ids(
    message: JsonObject,
    profile: ToolCallIdProfile,
    *,
    used_wire_ids: set[str],
    reserved_valid_ids: set[str],
    message_index: int,
) -> dict[str, list[str]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return {}

    result_ids: dict[str, list[str]] = {}
    response_item_ids: dict[str, list[str]] = {}
    for tool_call_index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            continue
        original_id = tool_call.get("id")
        if not isinstance(original_id, str) or not original_id:
            continue
        wire_id = _allocate_tool_call_id(
            original_id,
            profile,
            used_wire_ids=used_wire_ids,
            reserved_valid_ids=reserved_valid_ids,
            occurrence=f"{message_index}:{tool_call_index}",
        )
        tool_call["id"] = wire_id
        result_ids.setdefault(original_id, []).append(wire_id)
        response_item_ids.setdefault(original_id, []).append(wire_id)

    if profile.rewrite_responses_output_items:
        _rewrite_responses_output_tool_call_ids(message, response_item_ids)
    return result_ids


def _allocate_tool_call_id(
    original_id: str,
    profile: ToolCallIdProfile,
    *,
    used_wire_ids: set[str],
    reserved_valid_ids: set[str],
    occurrence: str,
) -> str:
    if _tool_call_id_is_valid(original_id, profile) and original_id not in used_wire_ids:
        used_wire_ids.add(original_id)
        return original_id

    attempt = 0
    while True:
        seed = original_id if attempt == 0 else f"{original_id}\0{occurrence}\0{attempt}"
        candidate = _derive_tool_call_id(original_id, seed, profile)
        if candidate not in used_wire_ids and candidate not in reserved_valid_ids:
            used_wire_ids.add(candidate)
            return candidate
        attempt += 1


def _tool_call_id_is_valid(tool_call_id: str, profile: ToolCallIdProfile) -> bool:
    if not tool_call_id or any(
        character not in profile.allowed_characters for character in tool_call_id
    ):
        return False
    if len(tool_call_id) > profile.max_length:
        return False
    if profile.exact_length is not None and len(tool_call_id) != profile.exact_length:
        return False
    return not (profile.trim_trailing_underscores and tool_call_id.endswith("_"))


def _derive_tool_call_id(
    original_id: str,
    seed: str,
    profile: ToolCallIdProfile,
) -> str:
    digest = hashlib.sha256(f"{profile.name}\0{seed}".encode()).hexdigest()
    if profile.exact_length is not None:
        return digest[: profile.exact_length]

    sanitized = "".join(
        character for character in original_id if character in profile.allowed_characters
    )
    if profile.trim_trailing_underscores:
        sanitized = sanitized.rstrip("_")
    digest_part = digest[: min(_TOOL_CALL_ID_HASH_LENGTH, profile.max_length)]
    if len(digest_part) == profile.max_length:
        return digest_part
    separator = "_" if "_" in profile.allowed_characters else ""
    prefix_length = profile.max_length - len(separator) - len(digest_part)
    prefix = sanitized[:prefix_length]
    if profile.trim_trailing_underscores:
        prefix = prefix.rstrip("_")
    if not prefix:
        return digest_part
    return f"{prefix}{separator}{digest_part}"


def _rewrite_responses_output_tool_call_ids(
    message: JsonObject,
    mapped_ids: dict[str, list[str]],
) -> None:
    reasoning_meta = message.get("reasoning_meta")
    if not isinstance(reasoning_meta, dict):
        return
    response_output = reasoning_meta.get(_RESPONSES_OUTPUT_META_KEY)
    if not isinstance(response_output, list):
        return

    remaining_ids = {original_id: list(wire_ids) for original_id, wire_ids in mapped_ids.items()}
    for item in response_output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        original_id = item.get("call_id")
        if not isinstance(original_id, str) or not original_id:
            original_id = item.get("id")
        if not isinstance(original_id, str):
            continue
        wire_ids = remaining_ids.get(original_id)
        if not wire_ids:
            continue
        wire_id = wire_ids.pop(0)
        if wire_id == original_id:
            continue
        item["call_id"] = wire_id
        # A Responses output item id is Provider-owned pairing state, separate
        # from ``call_id``. Replaying it after changing the call identity can
        # falsely pair a foreign function item with opaque reasoning.
        item.pop("id", None)
