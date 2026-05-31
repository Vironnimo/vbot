"""Shared RPC request validation helpers."""

from __future__ import annotations

from typing import Any

from core.chat.content_blocks import ContentBlock, ContentBlockError, content_block_from_dict
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]


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


def _required_string(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _required_bool(params: JsonObject, key: str) -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _required_integer_list(params: JsonObject, key: str) -> list[int]:
    value = params.get(key)
    if not isinstance(value, list):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of integers")

    parsed: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must be a list of integers",
            )
        parsed.append(item)
    return parsed


def _optional_integer_list(params: JsonObject, key: str, *, default: list[int]) -> list[int]:
    if key not in params:
        return list(default)
    return _required_integer_list(params, key)


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
