"""Shared RPC request validation helpers."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Any, Literal, cast

from core.chat import ChatError
from core.chat.content_blocks import ContentBlock, ContentBlockError, content_block_from_dict
from core.chat.model_resolution import parse_model_with_connection
from core.projects import InvalidAgentAddressError, parse_agent_address
from core.settings import is_valid_agent_id
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]
CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION = "speech_transcription"
CHAT_INPUT_ORIGINS = frozenset((CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION,))
ChatInputOrigin = Literal["speech_transcription"]


def _parse_chat_content(params: JsonObject, key: str) -> str | list[ContentBlock]:
    value = params.get(key)
    if isinstance(value, str):
        if value:
            return value
    elif isinstance(value, list):
        parsed_blocks: list[ContentBlock] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"params.{key}[{index}] must be an object",
                )
            try:
                parsed_blocks.append(content_block_from_dict(item))
            except ContentBlockError as exc:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"params.{key}[{index}] is invalid: {exc}",
                ) from exc
        return parsed_blocks

    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        f"params.{key} must be a non-empty string or a list of content blocks",
    )


def _optional_file_mentions(params: JsonObject) -> list[str]:
    """Read the optional ``file_mentions`` list on the chat send/queue RPCs.

    Paths of files the user ``@``-mentioned in the composer, to be snapshotted
    into the message at send time. Absent → empty list (no expansion).
    """
    if "file_mentions" not in params:
        return []
    return _required_string_list(params, "file_mentions")


def _required_agent_address(params: JsonObject, key: str) -> tuple[str, str | None]:
    """Read a required agent-address param and split it into ``(agent_id, project_id)``.

    The outside ``agent@projekt`` form is parsed at the system edge through the
    single :func:`core.projects.parse_agent_address` seam: a bare value yields a
    ``None`` project (identity, byte-identical to before), a qualified value
    yields the project id. A malformed address is a client error, surfaced as
    ``invalid_request`` rather than leaking a domain error.
    """
    raw = _required_string(params, key)
    try:
        return parse_agent_address(raw)
    except InvalidAgentAddressError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _optional_chat_input_origin(params: JsonObject) -> ChatInputOrigin | None:
    value = params.get("input_origin")
    if value is None:
        return None
    if not isinstance(value, str) or value not in CHAT_INPUT_ORIGINS:
        allowed = ", ".join(sorted(CHAT_INPUT_ORIGINS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.input_origin must be one of: {allowed}",
        )
    return cast(ChatInputOrigin, value)


def _required_string(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _required_block_slug(params: JsonObject, key: str) -> str:
    """Read a required custom-block slug and validate it with the canonical rule.

    A ``user:<slug>`` custom prompt block's ``<slug>`` follows the same
    filesystem-safe rule as an agent id (letters/digits, ``-``/``_``, leading
    alphanumeric, bounded length) — path-traversal-safe, the single edge gate
    behind the store's own re-validation (defense in depth). A bad slug is a client
    error surfaced as ``invalid_request``.
    """
    value = _required_string(params, key)
    if not is_valid_agent_id(value):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be a valid block slug (letters, digits, '-', '_')",
        )
    return value


def _required_bool(params: JsonObject, key: str) -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _required_string_list(params: JsonObject, key: str) -> list[str]:
    value = params.get(key)
    if not isinstance(value, list):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of non-empty strings"
        )

    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must be a list of non-empty strings",
            )
        parsed.append(item)
    return parsed


def _optional_string_list(params: JsonObject, key: str, *, default: list[str]) -> list[str]:
    if key not in params:
        return list(default)
    return _required_string_list(params, key)


def _optional_string(params: JsonObject, key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _optional_positive_integer(
    params: JsonObject, key: str, *, max_value: int | None = None
) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a positive integer")
    if max_value is not None and value > max_value:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be less than or equal to {max_value}",
        )
    return value


def _optional_bool(params: JsonObject, key: str, *, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _reject_unsupported(params: JsonObject, allowed: AbstractSet[str], method: str) -> None:
    """Raise ``invalid_request`` when *params* carries a field outside *allowed*."""
    unsupported_fields = sorted(set(params) - allowed)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported {method} fields: {', '.join(unsupported_fields)}",
        )


def _ensure_model_connection_supported(models: Any, label: str, model_string: str) -> None:
    """Reject a saved model whose pinned connection its allowlist forbids.

    Mirrors the task-target expansion rule (``Model.allows_connection``): a model
    with a non-empty connection allowlist may only run on the listed connection
    ids of its provider. This is the save-time guard behind the WebUI dropdown
    filter — it also catches model strings that never pass through the UI
    (imports, hand-edited config).

    Nothing is flagged when there is nothing to check: an empty value, no pinned
    connection (the runtime then picks a usable one), a malformed model string
    (surfaced elsewhere at run time), or a model absent from the catalog (e.g. a
    custom id). ``models`` is the runtime model registry (``runtime.models``).
    """
    if not model_string:
        return
    try:
        provider_id, model_id, connection_suffix = parse_model_with_connection(model_string)
    except ChatError:
        return
    if not connection_suffix:
        return

    connection_id = connection_suffix.partition(":")[0]
    try:
        model = models.get(provider_id, model_id)
    except KeyError:
        return
    if model.allows_connection(connection_id):
        return

    allowed = ", ".join(model.connections)
    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        f"params.{label}: model {provider_id}/{model_id} is not available on "
        f"connection '{connection_id}' (allowed connections: {allowed})",
    )


def _ensure_model_usable(state: Any, model: str) -> None:
    """Reject a model value that is not actually usable in this instance.

    Two gates, both surfaced as ``invalid_request`` and shared by the ``/model``
    command's set-time validation and ``project.set_override``'s model check so the
    accepted-model rule can never drift: a pinned ``::connection`` suffix must be
    allowed by the model's connection allowlist (checked first — its message names
    the forbidden connection), and the model must be configured here (provider
    registered, in catalog, a usable credential on an allowed connection — the
    resolver's public ``is_model_configured`` seam, the same rule behind the scan's
    ``BAD_MODEL`` finding, which itself re-checks the allowlist).
    """
    _ensure_model_connection_supported(state.runtime.models, "model", model)
    if not state.runtime.agent_resolver.is_model_configured(model):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"model {model!r} is not usable in this instance "
            "(unknown provider/model or no usable credential on an allowed connection)",
        )
