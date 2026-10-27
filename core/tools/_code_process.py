"""One Python cell, its private Tool bridge, bounded output and process lifetime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

import psutil  # type: ignore[import-untyped]

from core.tools._tool_results import tool_failure, tool_failure_for_exception, tool_success
from core.tools._tool_workers import run_tool_worker
from core.tools.contracts import JsonObject
from core.tools.process_tree import ProcessTree, track_process_tree
from core.utils.processes import (
    create_subprocess_exec,
    guarded_process_launch,
    subprocess_creation_flags,
    wait_for_exit,
)
from core.utils.workers import cancel_and_wait, finish_despite_cancel

MAX_TOOL_CALLS = 1000
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_FAILURES = 20
_CREATE_SUSPENDED = 0x00000004
type ToolInvoker = Callable[[str, JsonObject, list[Any]], Awaitable[JsonObject]]
type ExecutionStatus = Literal["completed", "failed", "timed_out", "stopped"]


@dataclass(frozen=True)
class CodeExecutionResult:
    status: ExecutionStatus
    exit_code: int | None
    output: str
    stderr: str
    tool_calls: int
    tool_failure_count: int
    tool_failures: list[JsonObject]
    output_truncated: bool = False
    stderr_truncated: bool = False
    output_path: Path | None = None
    log_truncated: bool = False
    # False when Python refused to compile the program, so none of it ran.
    ran: bool = True


@dataclass
class _Capture:
    limit: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total: int = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        missing = max(0, self.limit // 2 - len(self.head))
        self.head.extend(chunk[:missing])
        self.tail.extend(chunk[missing:])
        if len(self.tail) > self.limit - len(self.head):
            del self.tail[: len(self.tail) - (self.limit - len(self.head))]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def text(self) -> str:
        marker = (
            f"\n[... {self.total - self.limit} output bytes omitted ...]\n".encode()
            if self.truncated
            else b""
        )
        return (bytes(self.head) + marker + bytes(self.tail)).decode("utf-8", errors="replace")


class _Bridge:
    def __init__(self, invoke: ToolInvoker) -> None:
        self.invoke = invoke
        self.token = secrets.token_urlsafe(32)
        self.finished: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.tasks: set[asyncio.Task[None]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.calls = 0
        self.failure_count = 0
        self.failures: list[JsonObject] = []
        self.closed = False
        self.ran = True

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer), name="code-tool-bridge")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            if self.closed:
                return
            async with asyncio.timeout(10):
                line = await reader.readline()
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                return
            request = await run_tool_worker(json.loads, line)
            if not isinstance(request, dict) or not isinstance(request.get("token"), str):
                return
            if not secrets.compare_digest(request["token"], self.token):
                return
            if request.get("operation") == "finish":
                code = request.get("exit_code")
                if isinstance(code, int) and not self.finished.done():
                    self.ran = request.get("ran") is not False
                    self.finished.set_result(code)
                result = tool_success({})
            else:
                result = await self._call(request)
            encoded = await run_tool_worker(_encode, result)
            if len(encoded) > MAX_RESPONSE_BYTES:
                name = str(request.get("name", "Tool"))
                result = tool_failure(
                    "code_result_too_large",
                    "The Tool ran, but its result exceeds the Python bridge's 32 MiB limit. "
                    "Request a smaller page or range. Check the current state before "
                    "repeating a call that changes anything.",
                )
                self._record_failure(name, result)
                encoded = _encode(result)
            writer.write(encoded)
            await writer.drain()
        except OSError, ValueError, TimeoutError:
            # An incomplete/unauthenticated connection owns no authority. If its
            # response is lost after dispatch, the client reports uncertain effects.
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _call(self, request: dict[str, Any]) -> JsonObject:
        name, arguments = request.get("name"), request.get("arguments")
        if request.get("operation") != "call" or not isinstance(name, str):
            return tool_failure("invalid_arguments", "A Tool call needs its name and arguments.")
        positional = request.get("positional", [])
        if not isinstance(arguments, dict) or not isinstance(positional, list):
            return tool_failure("invalid_arguments", "Tool arguments must be a Python dictionary.")
        if self.closed or self.finished.done():
            return tool_failure("code_execution_ended", "The Python execution has ended.")
        if self.calls >= MAX_TOOL_CALLS:
            result = tool_failure(
                "code_tool_limit",
                f"The Python execution reached its {MAX_TOOL_CALLS} Tool-call limit.",
            )
            self._record_failure(name, result)
            return result
        self.calls += 1
        try:
            result = await self.invoke(name, arguments, positional)
        except asyncio.CancelledError:
            self._record_failure(
                name,
                tool_failure(
                    "cancelled", "The Python execution ended while this Tool call was unfinished."
                ),
            )
            raise
        except Exception as error:
            result = tool_failure_for_exception(name, error)
        if result.get("ok") is not True:
            self._record_failure(name, result)
        return result

    def _record_failure(self, name: str, result: JsonObject) -> None:
        self.failure_count += 1
        if len(self.failures) < MAX_FAILURES:
            error = result.get("error") or {}
            self.failures.append(
                {
                    "tool": name,
                    "code": error.get("code"),
                    "message": str(error.get("message"))[:2000],
                }
            )

    async def close(self) -> None:
        self.closed = True
        tasks = tuple(self.tasks)
        try:
            await cancel_and_wait(*tasks)
        finally:
            for writer in tuple(self.writers):
                writer.close()
            self.writers.clear()


def _encode(value: JsonObject) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )


async def run_python(
    code: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    invoke: ToolInvoker,
    timeout_seconds: float = 300,
    output_path: Path | None = None,
    stop: asyncio.Event | None = None,
) -> CodeExecutionResult:
    """Execute a fresh cell; cancelling this await stops and drains everything it owns.

    *environment* and *cwd* have already passed the caller's normal process
    permission checks. The optional caller-owned output file receives stdout and
    stderr, capped at 8 MiB; inline captures retain each stream's head and tail.
    Setting *stop* ends the cell like cancellation but returns its result as
    ``stopped``, with the output captured until then.
    """
    bridge = _Bridge(invoke)
    stdout, stderr = _Capture(MAX_OUTPUT_BYTES), _Capture(MAX_STDERR_BYTES)
    log: BinaryIO | None = None
    log_bytes = 0
    log_truncated = False
    log_lock = asyncio.Lock()
    process: asyncio.subprocess.Process | None = None
    tree: ProcessTree | None = None
    output_tasks: list[asyncio.Task[None]] = []
    exit_task: asyncio.Task[int] | None = None
    stop_task: asyncio.Task[Literal[True]] | None = None
    program_dir: Path | None = None
    status: ExecutionStatus = "failed"
    exit_code: int | None = None
    server = await asyncio.start_server(bridge.accept, "127.0.0.1", 0, limit=MAX_REQUEST_BYTES + 1)

    async def capture(stream: asyncio.StreamReader, retained: _Capture) -> None:
        nonlocal log_bytes, log_truncated
        while chunk := await stream.read(65536):
            retained.append(chunk)
            if log is not None:
                async with log_lock:
                    kept = chunk[: max(0, MAX_LOG_BYTES - log_bytes)]
                    log_truncated |= len(kept) < len(chunk)
                    if kept:
                        await run_tool_worker(log.write, kept)
                        log_bytes += len(kept)

    def created(pid: int) -> None:
        nonlocal tree
        tree = track_process_tree(pid)
        if sys.platform == "win32":
            psutil.Process(pid).resume()

    async def launch() -> None:
        nonlocal process
        argv = [
            sys.executable,
            "-I",
            "-B",
            "-u",
            "-X",
            "utf8",
            str(Path(__file__).with_name("_code_worker.py")),
        ]
        wrapped = guarded_process_launch(argv, env=environment)
        options: dict[str, Any] = (
            {"creationflags": subprocess_creation_flags(new_process_group=True) | _CREATE_SUSPENDED}
            if sys.platform == "win32"
            else {"start_new_session": True, "pass_fds": wrapped.pass_fds}
        )
        process = await create_subprocess_exec(
            *wrapped.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(environment),
            on_created=created,
            **options,
        )

    async def cleanup() -> None:
        server.close()
        bridge.closed = True
        failure: BaseException | None = None
        try:
            if tree is not None:
                try:
                    await run_tool_worker(tree.terminate)
                except OSError as error:
                    failure = error
                # A Windows Job's kill-on-close is also needed when terminate fails.
                try:
                    await run_tool_worker(tree.close)
                except OSError as error:
                    failure = failure or error
            await bridge.close()
            await server.wait_closed()
            if process is not None and (failure is None or sys.platform == "win32"):
                try:
                    async with asyncio.timeout(5):
                        await process.wait()
                except TimeoutError:
                    failure = failure or OSError(
                        f"The Python process {process.pid} did not finish closing after its tree "
                        "was stopped. Its termination could not be confirmed."
                    )
            if failure is not None:
                close_pipes()
            if output_tasks:
                outcomes = await asyncio.gather(*output_tasks, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        failure = failure or outcome
            if failure is not None:
                raise failure
        finally:
            close_pipes()
            if process is not None and process.returncode is not None:
                cast(Any, process)._transport.close()
            for task in (exit_task, stop_task):
                if task is not None:
                    await cancel_and_wait(task)
            for output_task in output_tasks:
                output_task.cancel()
            await asyncio.gather(*output_tasks, return_exceptions=True)
            if log is not None:
                await run_tool_worker(log.close)
            if program_dir is not None:
                await run_tool_worker(shutil.rmtree, program_dir, ignore_errors=True)

    def close_pipes() -> None:
        if process is None:
            return
        transport = cast(Any, process)._transport
        for number in (0, 1, 2):
            pipe = transport.get_pipe_transport(number)
            if pipe is not None:
                if number:
                    pipe.resume_reading()
                pipe.close()
        # Do not close a live POSIX guardian's root transport after a refused
        # kill: it must retain custody and keep trying to stop its descendants.

    try:
        if output_path is not None:
            log = await run_tool_worker(output_path.open, "wb")
        program_dir = Path(await run_tool_worker(tempfile.mkdtemp, prefix="vbot-code-"))
        program_path = program_dir / "program.py"
        # Unchanged source: no newline translation of the program's own text.
        await run_tool_worker(program_path.write_text, code, encoding="utf-8", newline="")
        async with asyncio.timeout(timeout_seconds):
            # A cancelled launch must still publish its process/tree to cleanup.
            launch_task = asyncio.create_task(launch())
            try:
                await asyncio.shield(launch_task)
            except asyncio.CancelledError:
                await finish_despite_cancel(launch_task)
                raise
            assert process is not None and process.stdin is not None
            assert process.stdout is not None and process.stderr is not None
            output_tasks = [
                asyncio.create_task(capture(process.stdout, stdout)),
                asyncio.create_task(capture(process.stderr, stderr)),
            ]
            assert server.sockets is not None
            bootstrap = {
                "path": str(program_path),
                "port": server.sockets[0].getsockname()[1],
                "token": bridge.token,
                "timeout": timeout_seconds,
            }
            process.stdin.write(_encode(bootstrap))
            await process.stdin.drain()
            process.stdin.close()
            exit_task = asyncio.create_task(wait_for_exit(process))
            waiting: set[asyncio.Future[Any]] = {bridge.finished, exit_task, *output_tasks}
            if stop is not None:
                stop_task = asyncio.create_task(stop.wait())
                waiting.add(stop_task)
            while True:
                done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                for output_task in output_tasks:
                    if output_task in done:
                        output_task.result()  # A failed capture must stop the child immediately.
                if bridge.finished in done or exit_task in done:
                    exit_code = (
                        bridge.finished.result() if bridge.finished in done else exit_task.result()
                    )
                    status = "completed" if exit_code == 0 else "failed"
                    break
                if stop_task in done:
                    status = "stopped"
                    break
    except TimeoutError:
        status = "timed_out"
    finally:
        await finish_despite_cancel(cleanup())
    return CodeExecutionResult(
        status=status,
        exit_code=exit_code,
        output=stdout.text(),
        stderr=stderr.text(),
        tool_calls=bridge.calls,
        tool_failure_count=bridge.failure_count,
        tool_failures=list(bridge.failures),
        output_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        output_path=output_path,
        log_truncated=log_truncated,
        ran=bridge.ran,
    )
