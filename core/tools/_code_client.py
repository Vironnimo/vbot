"""Standard-library Tool client, copied beside each program as vbot_tools.py."""

from __future__ import annotations

import datetime
import json
import os
import socket
from collections.abc import Callable, Mapping
from typing import Any, override

_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_REQUEST_BYTES = 2 * 1024 * 1024


class ToolError(Exception):
    """A Tool refused or failed; ``result`` retains its complete error envelope."""

    def __init__(self, name: str, result: dict[str, Any]) -> None:
        self.tool = name
        self.result = result
        error = result.get("error") or {}
        self.code = str(error.get("code", "tool_execution_error"))
        self.message = str(error.get("message", "The Tool returned no usable result."))
        super().__init__(f"{name}: {self.message}")

    @override
    def __reduce__(self) -> tuple[type[ToolError], tuple[str, dict[str, Any]]]:
        return type(self), (self.tool, self.result)


class ToolData(dict[str, Any]):
    """Decoded Tool data; a missing key names the keys the result has."""

    def __missing__(self, key: Any) -> Any:
        keys = ", ".join(map(str, self)) or "none"
        raise KeyError(f"{key!r} is not in this Tool result. Its keys: {keys}")


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _json_form(value: Any) -> Any:
    """The one obvious JSON form of a common Python value, such as a Path."""
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, set | frozenset):
        return list(value)
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class _Tools:
    def __init__(self, port: int, token: str, timeout: float) -> None:
        self._port = port
        self._token = token
        self._timeout = timeout

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("name", "execute_code"))
        try:
            payload = json.dumps(
                {**request, "token": self._token}, allow_nan=False, default=_json_form
            ).encode("utf-8")
            if len(payload) + 1 > _MAX_REQUEST_BYTES:
                raise ValueError("The arguments exceed the Python bridge's 2 MiB request limit.")
        except (TypeError, ValueError) as error:
            raise ToolError(
                name,
                _failure(
                    "invalid_arguments",
                    "Nothing was sent: Tool arguments must be finite JSON data within 2 MiB. "
                    f"Convert unsupported values or reduce the input. {error}",
                ),
            ) from None
        # One connection per invocation keeps ordinary Python threads independent.
        # Never reconnect/replay after a send: an unanswered mutation may have run.
        try:
            connection = socket.create_connection(("127.0.0.1", self._port), timeout=10)
        except OSError as error:
            raise ToolError(
                name,
                _failure(
                    "tool_bridge_unavailable",
                    f"Nothing was sent: the Python Tool bridge is unavailable. {error}",
                ),
            ) from None
        with connection:
            connection.settimeout(self._timeout)
            connection.sendall(payload + b"\n")
            with connection.makefile("rb") as incoming:
                answer = incoming.readline(_MAX_RESPONSE_BYTES + 1)
        if not answer.endswith(b"\n") or len(answer) > _MAX_RESPONSE_BYTES:
            raise OSError("The Tool bridge returned no complete response.")
        result = json.loads(answer, object_hook=ToolData)
        if not isinstance(result, dict):
            raise OSError("The Tool bridge returned no result object.")
        return result

    def call(self, name: str, /, *values: Any, **kwargs: Any) -> Any:
        """Call a Tool by name, with values in parameter order, keywords or one dictionary."""
        inputs: dict[str, Any] = {}
        if len(values) == 1 and isinstance(values[0], Mapping):
            inputs, values = dict(values[0]), ()
        for key, value in kwargs.items():
            if key in inputs and inputs[key] != value:
                raise ToolError(
                    name,
                    _failure(
                        "invalid_arguments",
                        f"Nothing was sent: {name} received {key} twice with different values, "
                        "in the dictionary and as a keyword. Pass it once.",
                    ),
                )
            inputs[key] = value
        request: dict[str, Any] = {"operation": "call", "name": name, "arguments": inputs}
        if values:
            # The host knows the Tool's parameters and their order.
            request["positional"] = list(values)
        try:
            result = self._request(request)
        except (OSError, ValueError) as error:
            raise ToolError(
                name,
                {
                    "ok": False,
                    "error": {
                        "code": "tool_call_unconfirmed",
                        "message": "The Tool connection ended without a usable result. "
                        "Whether the call took effect is unknown. Check its state before "
                        f"repeating a change. Detail: {error}",
                    },
                    "data": None,
                    "artifacts": [],
                },
            ) from None
        if result.get("ok") is not True:
            raise ToolError(name, result)
        return result.get("data")

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(*values: Any, **kwargs: Any) -> Any:
            return self.call(name, *values, **kwargs)

        invoke.__name__ = name
        return invoke


# Each process imports its own client; every invocation opens its own connection.
tools = _Tools(
    int(os.environ["VBOT_CODE_PORT"]),
    os.environ["VBOT_CODE_TOKEN"],
    float(os.environ["VBOT_CODE_TIMEOUT"]) + 10,
)
__getattr__ = tools.__getattr__
