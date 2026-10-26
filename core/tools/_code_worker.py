"""Standard-library child for execute_code; never imports the vBot runtime.

The host owns this process and every Tool call. The single bootstrap document
arrives on stdin; user code then sees EOF, ordinary Python, and ``vbot_tools``.
The program runs as the ``__main__`` script of its host-written file, so
``multiprocessing`` can import its functions in child processes.
"""

from __future__ import annotations

import atexit
import json
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


# Tracebacks name the class as the program imports it.
ToolError.__module__ = "vbot_tools"


class _ResultData(dict[str, Any]):
    """Decoded Tool data; a missing key names the keys the result has."""

    def __missing__(self, key: Any) -> Any:
        keys = ", ".join(map(str, self)) or "none"
        raise KeyError(f"{key!r} is not in this Tool result. Its keys: {keys}")


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
        result = json.loads(answer, object_hook=_ResultData)
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


def _run_main(program: types.CodeType, path: str) -> int:
    """Run *program* like ``python path``: as ``__main__``, then Python's exit steps."""
    module = types.ModuleType("__main__")
    module.__dict__.update(__file__=path, __builtins__=__builtins__)
    sys.modules["__main__"] = module
    sys.argv = [path]
    exit_code = 0
    try:
        exec(program, module.__dict__)
    except SystemExit as error:
        exit_code = _exit_code(error)
    except BaseException as error:
        _print_program_error(error)
        exit_code = 1
    # Like interpreter exit: wait for non-daemon threads, then atexit handlers.
    try:
        threading._shutdown()  # type: ignore[attr-defined]
        atexit._run_exitfuncs()
    except SystemExit as error:
        exit_code = _exit_code(error)
    except BaseException as error:
        _print_program_error(error)
        exit_code = exit_code or 1
    return exit_code


def _print_program_error(error: BaseException) -> None:
    """Print *error* like Python, without this worker's frames: only the program's."""
    report = traceback.TracebackException.from_exception(error)
    pending, seen = [report], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        item.stack = traceback.StackSummary.from_list(
            [frame for frame in item.stack if frame.filename != __file__]
        )
        pending.extend(
            linked
            for linked in (item.__cause__, item.__context__, *(item.exceptions or ()))
            if linked is not None
        )
    print("".join(report.format()), end="", file=sys.stderr)


def _exit_code(error: SystemExit) -> int:
    if isinstance(error.code, int):
        return error.code
    if error.code is not None:
        print(error.code, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    bootstrap = json.load(sys.stdin)
    tools = _Tools(bootstrap["port"], bootstrap["token"], bootstrap["timeout"] + 10)
    module = types.ModuleType("vbot_tools")
    module.__dict__.update(tools=tools, ToolError=ToolError, __getattr__=tools.__getattr__)
    sys.modules["vbot_tools"] = module
    # Trusted bootstrap imports finish before the project enters the import path.
    sys.path.insert(0, os.getcwd())
    path = bootstrap["path"]
    ran = False
    try:
        with open(path, encoding="utf-8") as file:
            program = compile(file.read(), path, "exec")
    except (SyntaxError, ValueError) as error:
        # Nothing ran; show only the error and its location, not this worker.
        traceback.print_exception(type(error), error, None)
        exit_code = 1
    else:
        ran = True
        exit_code = _run_main(program, path)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    try:
        tools._request({"operation": "finish", "exit_code": exit_code, "ran": ran})
        # Keep the root alive until the host has stopped its complete process tree.
        # This also prevents daemon threads from outliving the cell.
        threading.Event().wait()
    except OSError, ValueError:
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
