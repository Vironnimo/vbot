"""Standard-library child for execute_code; never imports the vBot runtime.

The host owns this process and every Tool call. The single bootstrap document
arrives on stdin; user code then sees EOF, ordinary Python, and ``vbot_tools``.
"""

from __future__ import annotations

import json
import linecache
import os
import socket
import sys
import threading
import traceback
import types
from collections.abc import Callable
from typing import Any

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


class _Tools:
    def __init__(self, port: int, token: str, timeout: float) -> None:
        self._port = port
        self._token = token
        self._timeout = timeout

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("name", "execute_code"))
        try:
            payload = json.dumps({**request, "token": self._token}, allow_nan=False).encode("utf-8")
            if len(payload) + 1 > _MAX_REQUEST_BYTES:
                raise ValueError("The arguments exceed the Python bridge's 2 MiB request limit.")
        except (TypeError, ValueError) as error:
            raise ToolError(
                name,
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_arguments",
                        "message": "Nothing was sent: Tool arguments must be finite JSON data "
                        f"within 2 MiB. Convert unsupported values or reduce the input. {error}",
                    },
                },
            ) from None
        # One connection per invocation keeps ordinary Python threads independent.
        # Never reconnect/replay after a send: an unanswered mutation may have run.
        try:
            connection = socket.create_connection(("127.0.0.1", self._port), timeout=10)
        except OSError as error:
            raise ToolError(
                name,
                {
                    "ok": False,
                    "error": {
                        "code": "tool_bridge_unavailable",
                        "message": "Nothing was sent: the Python Tool bridge is unavailable. "
                        f"{error}",
                    },
                },
            ) from None
        with connection:
            connection.settimeout(self._timeout)
            connection.sendall(payload + b"\n")
            with connection.makefile("rb") as incoming:
                answer = incoming.readline(_MAX_RESPONSE_BYTES + 1)
        if not answer.endswith(b"\n") or len(answer) > _MAX_RESPONSE_BYTES:
            raise OSError("The Tool bridge returned no complete response.")
        result = json.loads(answer)
        if not isinstance(result, dict):
            raise OSError("The Tool bridge returned no result object.")
        return result

    def call(self, name: str, arguments: dict[str, Any] | None = None, /, **kwargs: Any) -> Any:
        """Call a Tool by name, with its ordinary named arguments."""
        inputs = dict(arguments or {})
        if inputs.keys() & kwargs.keys():
            raise TypeError("Pass each Tool argument once, in the dictionary or as a keyword.")
        inputs.update(kwargs)
        try:
            result = self._request({"operation": "call", "name": name, "arguments": inputs})
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

        def invoke(arguments: dict[str, Any] | None = None, /, **kwargs: Any) -> Any:
            return self.call(name, arguments, **kwargs)

        invoke.__name__ = name
        return invoke


def main() -> int:
    bootstrap = json.load(sys.stdin)
    tools = _Tools(bootstrap["port"], bootstrap["token"], bootstrap["timeout"] + 10)
    module = types.ModuleType("vbot_tools")
    module.__dict__.update(tools=tools, ToolError=ToolError, __getattr__=tools.__getattr__)
    sys.modules["vbot_tools"] = module
    # Trusted bootstrap imports finish before the project enters the import path.
    sys.path.insert(0, os.getcwd())
    source = bootstrap["code"]
    filename = "<execute_code>"
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    exit_code = 0
    try:
        exec(compile(source, filename, "exec"), {"__name__": "__main__", "__file__": filename})
    except SystemExit as error:
        if isinstance(error.code, int):
            exit_code = error.code
        elif error.code is not None:
            print(error.code, file=sys.stderr)
            exit_code = 1
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    try:
        tools._request({"operation": "finish", "exit_code": exit_code})
        # Keep the root alive until the host has stopped its complete process tree.
        # This also prevents background Python threads from outliving the cell.
        threading.Event().wait()
    except OSError, ValueError:
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
