"""Shared RPC request validation helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Set as AbstractSet
from typing import Any, Literal, cast

from core.chat import ChatError, ReplySurface
from core.chat.content_blocks import ContentBlock, ContentBlockError, content_block_from_dict
from core.chat.model_resolution import parse_model_with_connection
from core.projects import (
    AGENT_OVERRIDE_FIELDS,
    AgentOverrides,
    InvalidAgentAddressError,
    parse_agent_address,
)
from core.sessions import AGENT_DEFAULT_PROJECT, SessionAddress, WorkingProjectChoice
from core.settings import is_valid_agent_id, is_valid_project_id
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]
CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION = "speech_transcription"
CHAT_INPUT_ORIGIN_LIVE_VOICE = "live_voice"
CHAT_INPUT_ORIGINS = frozenset(
    (CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION, CHAT_INPUT_ORIGIN_LIVE_VOICE)
)
ChatInputOrigin = Literal["speech_transcription", "live_voice"]


def _chat_reply_surface(params: JsonObject) -> ReplySurface:
    """Read the destination of this submission, independent of the Session's past."""
    value = params.get("reply_surface", "webui")
    if value == "webui":
        return ReplySurface.webui()
    if value == "tui":
        return ReplySurface.tui()
    raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.reply_surface must be webui or tui")


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


MAX_WIDGET_STATES = 8
MAX_WIDGET_STATE_BYTES = 8 * 1024
_WIDGET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _optional_widget_states(params: JsonObject) -> list[tuple[str, JsonObject]]:
    """Read the optional ``widget_states`` list on the chat send RPCs.

    ``[{widget_id, state}]``: values the user changed in Agent widgets, sent
    with this message. Absent -> empty list. Each ``state`` is a JSON object of
    at most ``MAX_WIDGET_STATE_BYTES`` as compact UTF-8 JSON; ids appear at most once.
    """
    if "widget_states" not in params:
        return []
    value = params["widget_states"]
    if not isinstance(value, list) or not value or len(value) > MAX_WIDGET_STATES:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.widget_states must be a list of 1 to {MAX_WIDGET_STATES} widget states",
        )
    parsed: list[tuple[str, JsonObject]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"widget_id", "state"}:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.widget_states items must be objects with widget_id and state",
            )
        widget_id, state = item["widget_id"], item["state"]
        if not isinstance(widget_id, str) or _WIDGET_ID.fullmatch(widget_id) is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "params.widget_states widget_id must be a widget id"
            )
        if any(widget_id == seen for seen, _ in parsed):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, f"params.widget_states names {widget_id} twice"
            )
        if not isinstance(state, dict):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "params.widget_states state must be a JSON object"
            )
        try:
            size = len(
                json.dumps(
                    state, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except TypeError, ValueError:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "params.widget_states state must be a JSON object"
            ) from None
        if size > MAX_WIDGET_STATE_BYTES:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.widget_states state must be at most {MAX_WIDGET_STATE_BYTES} bytes",
            )
        parsed.append((widget_id, state))
    return parsed


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


def _required_session_address(params: JsonObject) -> SessionAddress:
    """Parse one RPC Session address; internal helpers keep it intact."""
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    return SessionAddress(project_id, agent_id, session_id)


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
    """Reject a saved Model whose pinned Connection its allowlist forbids."""
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


def _validate_string_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of strings")
    return list(value)


def _optional_agent_overrides(
    container: JsonObject, *, allow_clear: bool, label: str = "params"
) -> JsonObject | None:
    """Read ``<label>.agent_overrides``: a map of Agent override fields to values.

    With *allow_clear* a ``null`` value clears that field; otherwise every value
    must be set. Values are validated here, the Model's usability by the resolver.
    """
    if "agent_overrides" not in container or container["agent_overrides"] is None:
        return None
    raw = container["agent_overrides"]
    key = f"{label}.agent_overrides"
    if not isinstance(raw, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{key} must be an object")
    supported = (*AGENT_OVERRIDE_FIELDS, "thinking_effort_default")
    unknown = sorted(set(raw) - set(supported))
    if unknown:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"{key} has unsupported fields: "
            + ", ".join(unknown)
            + "; supported: "
            + ", ".join(supported),
        )
    if not allow_clear and any(value is None for value in raw.values()):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{key} values must not be null here")
    try:
        AgentOverrides(**{name: value for name, value in raw.items() if value is not None})
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{key}: {exc}") from exc
    return dict(raw)


def _optional_working_project(
    container: JsonObject, project_id: str | None, *, label: str = "params"
) -> WorkingProjectChoice:
    """Read ``<label>.working_project_id``: the working Project a new Session starts in.

    Left out, the Session starts in its Agent's default Project; ``null`` names
    the Agent's Workspace, a string a Project (whose existence the resolver
    checks). A Team Agent address takes none: its Sessions work in its Project.
    """
    if "working_project_id" not in container:
        return AGENT_DEFAULT_PROJECT
    key = f"{label}.working_project_id"
    if project_id is not None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"{key} is not accepted for a Team Agent: its Sessions work in Project {project_id}",
        )
    value = container["working_project_id"]
    if value is None:
        return None
    if not isinstance(value, str) or not is_valid_project_id(value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{key} must be null or a valid Project id")
    return value


def _draft_working_project(
    params: JsonObject, project_id: str | None, session_id: str | None
) -> WorkingProjectChoice:
    """Read ``params.working_project_id`` for a request about a Session not created yet.

    An existing Session (``params.session_id``) works in its own Project, so a
    request names at most one of the two; see :func:`_optional_working_project`.
    """
    if session_id is not None and "working_project_id" in params:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.working_project_id and params.session_id exclude each other: "
            "an existing Session works in its own Project",
        )
    return _optional_working_project(params, project_id)
