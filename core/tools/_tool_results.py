"""Tool result envelopes, failures for exceptions, and media artifact validation."""

from __future__ import annotations

from typing import Any

from core.tools._tool_definitions import (
    InvalidToolResultError,
    SessionToolUnavailableError,
    ToolNotAllowedError,
    ToolNotFoundError,
)
from core.tools.contracts import JsonObject, ToolContractError
from core.tools.model_names import model_tool_name
from core.utils.logging import get_logger

_LOGGER = get_logger("tools")

TOOL_EXECUTION_ERROR_CODE = "tool_execution_error"
_EXECUTION_FAILURE_MESSAGE = (
    "{tool} failed while running: {cause} It is unknown how much of the call took effect. "
    "Check the current state before you call {tool} again."
)


def tool_success(data: JsonObject, artifacts: list[JsonObject] | None = None) -> JsonObject:
    """Return a stable success envelope for a tool result."""
    if not isinstance(data, dict):
        raise ValueError("Tool success data must be a JSON object")

    return {
        "ok": True,
        "error": None,
        "data": data,
        "artifacts": _copy_artifacts(artifacts),
    }


def tool_failure(
    code: str,
    message: str,
    artifacts: list[JsonObject] | None = None,
    *,
    retryable: bool | None = None,
    attempts_made: int | None = None,
) -> JsonObject:
    """Return a stable failure envelope for a tool result.

    ``retryable``/``attempts_made`` are optional retry-signalling fields that go
    *inside* the ``error`` object — never as top-level envelope keys, which would
    break ``is_tool_result_envelope`` (it checks the top-level key set exactly).
    They let a tool tell the model whether the failure is transient and how many
    attempts the tool already made before giving up, so the model does not
    pointlessly re-invoke a tool that has already exhausted its own retries.
    """
    if not code:
        raise ValueError("Tool failure code is required")
    if not message:
        raise ValueError("Tool failure message is required")
    if retryable is not None and not isinstance(retryable, bool):
        raise ValueError("Tool failure retryable must be a boolean or None")
    if attempts_made is not None and (
        isinstance(attempts_made, bool) or not isinstance(attempts_made, int) or attempts_made < 0
    ):
        raise ValueError("Tool failure attempts_made must be a non-negative integer or None")

    error: JsonObject = {"code": code, "message": message}
    if retryable is not None:
        error["retryable"] = retryable
    if attempts_made is not None:
        error["attempts_made"] = attempts_made

    return {
        "ok": False,
        "error": error,
        "data": None,
        "artifacts": _copy_artifacts(artifacts),
    }


def tool_failure_for_exception(tool_name: str, error: Exception) -> JsonObject:
    """Return the failure envelope for an exception that ended one Tool call.

    The error code follows what the exception says about the call's state:
    ``ToolContractError`` refused the call before it had any effect
    (``invalid_arguments``); the registry errors refused it before it ran; an
    ``InvalidToolResultError`` rejects what a finished handler returned. Every
    other exception escaped a running handler, so the call's effects are
    unknown (``tool_execution_error``); it is logged with its traceback.
    Building the envelope never raises, even for an exception without a message.
    """
    name = model_tool_name(tool_name)
    if isinstance(error, ToolContractError):
        return tool_failure(
            "invalid_arguments",
            _error_text(error) or f"{name} was not run: its arguments are invalid.",
        )
    if isinstance(error, ToolNotFoundError):
        return tool_failure("tool_not_found", _error_text(error) or f"Unknown Tool: {name}.")
    if isinstance(error, SessionToolUnavailableError):
        return tool_failure(
            f"{tool_name}_unavailable",
            _error_text(error) or f"Session tool unavailable: {name}",
        )
    if isinstance(error, ToolNotAllowedError):
        return tool_failure("tool_not_allowed", _error_text(error) or f"Tool not allowed: {name}")
    if isinstance(error, InvalidToolResultError):
        return tool_failure(
            "invalid_tool_result",
            _error_text(error) or f"Tool handler returned an invalid result: {name}",
        )
    _LOGGER.error("Tool %s crashed unexpectedly", tool_name, exc_info=error)
    return tool_failure(
        TOOL_EXECUTION_ERROR_CODE,
        _EXECUTION_FAILURE_MESSAGE.format(tool=name, cause=_sentence(_cause_text(error))),
    )


def _error_text(error: BaseException) -> str:
    return str(error).strip()


def _cause_text(error: BaseException) -> str:
    """Return the exception's message, or its type name when it has none."""
    return _error_text(error) or type(error).__name__


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


READ_MEDIA_ARTIFACT_KIND = "read_media"


def read_media_artifact(*, attachment_id: str, filename: str, media_type: str) -> JsonObject:
    """Build a ``read_media`` artifact describing a stored media blob.

    A Tool emits this artifact so Chat can attach the stored image as Run-local
    rich content on the correlated Tool Result. ``web_fetch`` and remote Tool
    media use this shared shape; local ``read`` images use in-memory content.
    """
    return {
        "kind": READ_MEDIA_ARTIFACT_KIND,
        "attachment_id": attachment_id,
        "filename": filename,
        "media_type": media_type,
    }


def is_tool_result_envelope(result: JsonObject) -> bool:
    """Return whether a JSON object matches the stable tool result envelope."""
    if set(result) != {"ok", "error", "data", "artifacts"}:
        return False
    if not isinstance(result["ok"], bool):
        return False
    if not isinstance(result["artifacts"], list):
        return False

    if result["ok"]:
        return result["error"] is None and isinstance(result["data"], dict)

    return result["data"] is None and _is_error_object(result["error"])


def _copy_artifacts(artifacts: list[JsonObject] | None) -> list[JsonObject]:
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise ValueError("Tool result artifacts must be a list")
    if not all(isinstance(artifact, dict) for artifact in artifacts):
        raise ValueError("Tool result artifacts must contain JSON objects")

    return [dict(artifact) for artifact in artifacts]


_REQUIRED_ERROR_KEYS = frozenset({"code", "message"})
_OPTIONAL_ERROR_KEYS = frozenset({"retryable", "attempts_made"})


def _is_error_object(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    keys = set(error)
    if not keys >= _REQUIRED_ERROR_KEYS or not keys <= (
        _REQUIRED_ERROR_KEYS | _OPTIONAL_ERROR_KEYS
    ):
        return False
    if not (isinstance(error["code"], str) and error["code"]):
        return False
    if not (isinstance(error["message"], str) and error["message"]):
        return False
    if "retryable" in error and not isinstance(error["retryable"], bool):
        return False
    if "attempts_made" in error:
        attempts_made = error["attempts_made"]
        if (
            isinstance(attempts_made, bool)
            or not isinstance(attempts_made, int)
            or attempts_made < 0
        ):
            return False
    return True
