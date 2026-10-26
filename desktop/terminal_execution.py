"""Desktop-owned PTYs and replayable output for a connected vBot server.

Only native process primitives are shared with core. Session ownership, Tool
policy, rendering, persistence and completion delivery stay on the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, cast

from core.tools._terminal_launch import (
    ShellLaunch,
    integrated_shell_kind,
    interactive_shell,
    shell_launch,
)
from core.tools.process_tree import ProcessTree, observe_process_tree
from core.tools.shell_environment import terminal_environment
from core.tools.terminal_backend import (
    TerminalAdapter,
    command_words,
    default_terminal_argv,
    spawn_terminal_adapter,
    terminate_process_tree,
)
from core.utils.file_status import is_dir_strict
from core.utils.processes import process_tree_runs

_MAX_PROCESSES = 32
_MAX_RETAINED_PROCESSES = 128
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_READ_BYTES = 64 * 1024
# One server input at its maximum, with the paste brackets it may carry.
_MAX_WRITE_CHARS = 2 * 64 * 1024
_LOGGER = logging.getLogger(__name__)


class TerminalExecutionError(RuntimeError):
    """A native failure whose known effect can be relayed to the server."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Process:
    """A PTY with a disk-backed replay window, independent of the connection."""

    def __init__(
        self,
        adapter: TerminalAdapter,
        *,
        observer: ProcessTree | None,
        cleanup: Callable[[], None],
        metadata: dict[str, Any],
    ) -> None:
        self.adapter = adapter
        self.observer = observer
        self.cleanup = cleanup
        self.metadata = metadata
        self._condition = threading.Condition()
        self._file = cast(BinaryIO, tempfile.TemporaryFile("w+b"))  # noqa: SIM115 - PTY lifetime
        self._base = 0
        self._end = 0
        self._eof = False
        self._error: str | None = None
        self._closed = False
        self._lifecycle = threading.RLock()
        self._targets: list[Any] = []
        self._reader = threading.Thread(
            target=self._pump, name="vbot-desktop-terminal", daemon=True
        )
        self._reader.start()

    def _pump(self) -> None:
        try:
            while not self._closed:
                try:
                    text = self.adapter.read(_MAX_READ_BYTES)
                except TimeoutError:
                    if not self.adapter.is_alive():
                        break
                    continue
                if not text:
                    continue
                data = text.encode("utf-8", errors="surrogateescape")
                with self._condition:
                    # A ring file bounds disk usage while offsets remain absolute.
                    start = self._end % _MAX_OUTPUT_BYTES
                    first = min(len(data), _MAX_OUTPUT_BYTES - start)
                    self._file.seek(start)
                    self._file.write(data[:first])
                    if first < len(data):
                        self._file.seek(0)
                        self._file.write(data[first:])
                    self._end += len(data)
                    self._base = max(0, self._end - _MAX_OUTPUT_BYTES)
                    self._condition.notify_all()
        except EOFError, OSError:
            pass
        except Exception as error:
            self._error = str(error)
        finally:
            with self._condition:
                self._eof = True
                self._condition.notify_all()

    def read(self, offset: int, size: int) -> dict[str, Any]:
        with self._condition:
            if self._closed:
                return {"output": "", "offset": offset, "next_offset": offset, "eof": True}
            if offset > self._end:
                raise ValueError("Terminal output offset is past the available output")
            if offset == self._end and not self._eof:
                self._condition.wait(0.25)
            if offset < self._base:
                raise ValueError("Terminal output replay expired while disconnected")
            count = min(size, self._end - offset)
            start = offset % _MAX_OUTPUT_BYTES
            first = min(count, _MAX_OUTPUT_BYTES - start)
            self._file.seek(start)
            data = self._file.read(first)
            if first < count:
                self._file.seek(0)
                data += self._file.read(count - first)
            # UTF-8 bytes are transported losslessly, including malformed output.
            import base64

            return {
                "output": base64.b64encode(data).decode("ascii"),
                "offset": offset,
                "next_offset": offset + len(data),
                "eof": self._eof and offset + len(data) == self._end,
                "error": self._error,
            }

    def status(self) -> dict[str, Any]:
        alive = self.adapter.is_alive()
        result: dict[str, Any] = {
            "alive": alive,
            "exit_code": self.adapter.exit_code(),
        }
        if self.observer is not None:
            try:
                facts = self.observer.facts()
            except Exception:
                # Process sampling is optional; it must never hide a known exit.
                return result
            else:
                result["processes"] = [{"pid": p.pid, "name": p.name} for p in facts.running]
                result["cpu_seconds"] = facts.cpu_seconds
                result["started"] = facts.started
        return result

    def kill(self) -> dict[str, Any]:
        with self._lifecycle:
            if not self._closed:
                terminate_process_tree(self.adapter, targets=self._targets)
            return {"alive": self.adapter.is_alive(), "exit_code": self.adapter.exit_code()}

    def close(self) -> None:
        with self._lifecycle:
            if self._closed:
                return
            self.kill()
            self.adapter.close()
            self._reader.join(timeout=2)
            if self._reader.is_alive():
                raise RuntimeError("Terminal output reader did not stop")
            with self._condition:
                self._closed = True
                self._file.close()
                self._condition.notify_all()
            if self.observer is not None:
                self.observer.close()
            self.cleanup()


class TerminalCapability:
    """Native terminal capability whose lifetime is the running Desktop app."""

    read_only_operations = frozenset({"read", "status"})

    def __init__(self) -> None:
        self._processes: dict[str, _Process] = {}
        self._starting: dict[str, asyncio.Task[_Process]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def active_count(self) -> int:
        return sum(process.adapter.is_alive() for process in tuple(self._processes.values()))

    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._execute(operation, params)
        except ValueError as error:
            raise TerminalExecutionError("invalid_arguments", str(error)) from error
        except FileNotFoundError as error:
            raise TerminalExecutionError(
                "terminal_command_not_found", f"No terminal was started: {error}"
            ) from error
        except OSError as error:
            if operation != "start":
                raise
            # A failed native spawn leaves no process behind: a definite start failure.
            raise TerminalExecutionError(
                "terminal_start_failed", f"No terminal was started: {error}"
            ) from error

    async def _execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closed and operation not in {"read", "status", "release", "kill"}:
            raise RuntimeError("The Desktop terminal capability is closed")
        if operation == "start":
            async with self._lock:
                if self._closed:
                    raise RuntimeError("The Desktop terminal capability is closed")
                process_id = params.get("process_id")
                if not isinstance(process_id, str) or not process_id or len(process_id) > 96:
                    raise ValueError("A terminal process_id is required")
                existing = self._processes.get(process_id)
                if existing is not None:
                    return {"process_id": process_id, **existing.metadata}
                if pending := self._starting.get(process_id):
                    process = await asyncio.shield(pending)
                    return {"process_id": process_id, **process.metadata}
                if self.active_count + len(self._starting) >= _MAX_PROCESSES:
                    raise ValueError("This Desktop already runs 32 terminals; close one first")
                if len(self._processes) + len(self._starting) >= _MAX_RETAINED_PROCESSES:
                    raise ValueError(
                        "This Desktop retains too many terminal results. "
                        "Reconnect so the server can collect them before starting another."
                    )
                # Publish ownership before starting the blocking native spawn.
                # Reconnect reads and kill/close must settle this same launch.
                starting = asyncio.create_task(self._create(process_id, params))
                self._starting[process_id] = starting
                process = await asyncio.shield(starting)
                return {"process_id": process_id, **process.metadata}
        process_id = params.get("process_id")
        if isinstance(process_id, str) and (pending := self._starting.get(process_id)):
            await asyncio.shield(pending)
        if not isinstance(process_id, str) or process_id not in self._processes:
            if operation in {"status", "kill"}:
                return {"alive": False, "exit_code": None, "missing": True}
            if operation == "read":
                offset = _integer(params, "offset", 0, 2**63 - 1)
                return {
                    "output": "",
                    "offset": offset,
                    "next_offset": offset,
                    "eof": True,
                    "missing": True,
                }
            if operation == "release":
                return {"released": True}
            raise ValueError("The terminal process is no longer present in this Desktop app")
        process = self._processes[process_id]
        if operation == "read":
            offset = _integer(params, "offset", 0, 2**63 - 1)
            size = _integer(params, "size", 1, _MAX_READ_BYTES, default=_MAX_READ_BYTES)
            result = await asyncio.to_thread(process.read, offset, size)
            if offset == 0:
                result["process"] = process.metadata
            return result
        if operation == "status":
            return {**await asyncio.to_thread(process.status), "process": process.metadata}
        if operation == "write":
            data = params.get("data")
            if not isinstance(data, str) or len(data) > _MAX_WRITE_CHARS:
                raise ValueError(
                    f"Terminal input must be text of at most {_MAX_WRITE_CHARS} characters"
                )
            await asyncio.to_thread(process.adapter.write, data)
            return {"written": len(data)}
        if operation == "resize":
            rows = _integer(params, "rows", 10, 80)
            columns = _integer(params, "columns", 40, 240)
            await asyncio.to_thread(process.adapter.resize, rows, columns)
            return {"rows": rows, "columns": columns}
        if operation == "kill":
            return await asyncio.to_thread(process.kill)
        if operation == "release":
            async with self._lock:
                if process.adapter.is_alive():
                    raise ValueError("A running terminal cannot be released")
                await asyncio.to_thread(process.close)
                self._processes.pop(process_id, None)
            return {"released": True}
        if operation == "program":
            program = params.get("program")
            if not isinstance(program, str) or not program:
                raise ValueError("A program name is required")
            running = await asyncio.to_thread(process_tree_runs, process.adapter.pid, program)
            return {"running": running}
        raise ValueError(f"Unknown terminal operation: {operation}")

    async def disconnected(self) -> None:
        """Keep PTYs and their output alive while the same server reconnects."""

    async def _create(self, process_id: str, params: dict[str, Any]) -> _Process:
        try:
            process = await asyncio.to_thread(self._start, params)
            self._processes[process_id] = process
            return process
        finally:
            self._starting.pop(process_id, None)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            # A cancelled call can leave its native spawn in flight. Its task
            # still owns publication, and must settle before close returns.
            if self._starting:
                await asyncio.gather(*tuple(self._starting.values()), return_exceptions=True)
            failures: list[BaseException] = []
            for key, process in list(self._processes.items()):
                try:
                    await asyncio.to_thread(process.close)
                except Exception as error:
                    failures.append(error)
                else:
                    del self._processes[key]
            if failures:
                raise RuntimeError(
                    "Some Desktop terminal processes could not be stopped"
                ) from failures[0]

    @staticmethod
    def _start(params: dict[str, Any]) -> _Process:
        command = params.get("command")
        args = params.get("args", [])
        if command is not None and (not isinstance(command, str) or not command.strip()):
            raise ValueError("Terminal command must name a program")
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or "\0" in arg for arg in args
        ):
            raise ValueError("Terminal arguments must be strings without NUL characters")
        raw_workdir = params.get("workdir")
        if raw_workdir is not None and not isinstance(raw_workdir, str):
            raise ValueError("Terminal workdir must be a path on this computer")
        cwd = Path(raw_workdir).expanduser() if raw_workdir else Path.home()
        if not cwd.is_absolute():
            cwd = Path.home() / cwd
        cwd = cwd.resolve()
        if not is_dir_strict(cwd):
            raise ValueError(f"No terminal was started: workdir {cwd} is not a directory")
        rows = _integer(params, "rows", 10, 80, default=24)
        columns = _integer(params, "columns", 40, 240, default=80)
        environment = terminal_environment()
        # A local program gets its own user's environment, never server paths/tokens.
        environment = {
            key: value for key, value in environment.items() if not key.startswith("VBOT_")
        }
        shell_argv = default_terminal_argv(environment)
        argv = [command, *args] if command else [*shell_argv, *args]
        notes: list[str] = []
        operator = params.get("operator") is True
        if (
            command
            and not operator
            and any(char.isspace() for char in command)
            and shutil.which(command, path=environment.get("PATH")) is None
            and not Path(command).is_file()
        ):
            try:
                words = command_words(command)
            except ValueError:
                words = []
            if len(words) > 1 and shutil.which(words[0], path=environment.get("PATH")):
                argv = [*words, *args]
                notes.append("command was read as a program and its arguments")
        integration: ShellLaunch | None = None
        adapter: TerminalAdapter | None = None
        observer: ProcessTree | None = None
        try:
            if operator and command:
                integration = shell_launch(shell_argv, command, args, environment=environment)
                shown_argv = shell_argv
            else:
                shown_argv = argv
                integration = interactive_shell(argv, environment=environment)
            shell = integrated_shell_kind(shown_argv)
            if integration is not None:
                argv = integration.argv
                environment.update(integration.environment)
            adapter = spawn_terminal_adapter(
                argv,
                cwd,
                environment,
                rows,
                columns,
                command_line=integration.command_line if integration else None,
            )
            try:
                observer = observe_process_tree(adapter.pid)
            except Exception:
                _LOGGER.warning("Desktop terminal process sampling is unavailable", exc_info=True)
            return _Process(
                adapter,
                observer=observer,
                cleanup=integration.remove_scratch if integration else lambda: None,
                metadata={
                    "pid": adapter.pid,
                    "command": shown_argv[0],
                    "args": list(shown_argv[1:]),
                    "workdir": str(cwd),
                    "shell": shell,
                    "note": " ".join(notes),
                },
            )
        except BaseException:
            if adapter is not None:
                with contextlib.suppress(OSError):
                    terminate_process_tree(adapter)
                adapter.close()
            if observer is not None:
                observer.close()
            if integration is not None:
                integration.remove_scratch()
            raise


def _integer(
    params: dict[str, Any], name: str, low: int, high: int, *, default: int | None = None
) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"Terminal {name} must be an integer between {low} and {high}")
    return value
