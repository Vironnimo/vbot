"""Standard-library child for execute_code; never imports the vBot runtime.

The host owns this process and every Tool call. The single bootstrap document
arrives on stdin; user code then sees EOF, ordinary Python, and ``vbot_tools``.
The program runs as the ``__main__`` script of its host-written file, so
``multiprocessing`` can import its functions in child processes.
"""

from __future__ import annotations

import atexit
import importlib.util
import io
import json
import linecache
import os
import sys
import threading
import traceback
import types


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
        _print_program_error(error, path)
        exit_code = 1
    # Like interpreter exit: wait for non-daemon threads, then atexit handlers.
    try:
        threading._shutdown()  # type: ignore[attr-defined]
        atexit._run_exitfuncs()
    except SystemExit as error:
        exit_code = _exit_code(error)
    except BaseException as error:
        _print_program_error(error, path)
        exit_code = exit_code or 1
    return exit_code


def _print_program_error(error: BaseException, path: str) -> None:
    """Print *error* like Python, without this worker's frames: only the program's."""
    report = traceback.TracebackException.from_exception(error)
    pending, seen = [report], set()
    internal = {__file__, os.path.join(os.path.dirname(path), "vbot_tools.py")}
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        item.stack = traceback.StackSummary.from_list(
            [frame for frame in item.stack if frame.filename not in internal]
        )
        pending.extend(
            linked
            for linked in (item.__cause__, item.__context__, *(item.exceptions or ()))
            if linked is not None
        )
    print("".join(report.format()).replace(path, "program.py"), end="", file=sys.stderr)


def _exit_code(error: SystemExit) -> int:
    if isinstance(error.code, int):
        return error.code
    if error.code is not None:
        print(error.code, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    bootstrap = json.load(sys.stdin)
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(newline="\n")
    path = bootstrap["path"]
    directory = os.path.dirname(path)
    # Load the host-written client before exposing project modules to imports.
    spec = importlib.util.spec_from_file_location(
        "vbot_tools", os.path.join(directory, "vbot_tools.py")
    )
    assert spec is not None and spec.loader is not None
    client = importlib.util.module_from_spec(spec)
    sys.modules["vbot_tools"] = client
    spec.loader.exec_module(client)
    tools = client.tools
    # Multiprocessing copies sys.path and inherits the bridge environment.
    sys.path[:0] = [directory, os.getcwd()]
    tools._request({"operation": "start"})
    ran = False
    try:
        with open(path, encoding="utf-8") as file:
            source = file.read()
        linecache.cache["program.py"] = (len(source), None, source.splitlines(True), "program.py")
        program = compile(source, "program.py", "exec")
    except (SyntaxError, ValueError) as error:
        # Nothing ran; show only the error and its location, not this worker.
        traceback.print_exception(type(error), error, None)
        exit_code = 1
    else:
        ran = True
        exit_code = _run_main(program, path)
    finally:
        for stream in (sys.stdout, sys.stderr):
            if not stream.closed:
                stream.flush()
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
