"""The long-lived PowerShell parser helper and the lowering of its answers.

One helper process per ``CommandAnalyzer``, started on first use with the
PowerShell executable the caller names. It runs ``powershell_analysis.ps1``,
which vBot hands over on stdin (so no execution policy applies and the script
ships as a plain resource file in the package), and answers one JSON line per
request. Requests are serialized, and a wait above a second is logged: one
helper of about 115 MB answers a typical line in a few milliseconds, far more
than the Tool calls need, so a second one would cost memory without gain. Each
request has a timeout, a failed request is retried once with a fresh helper,
and the helper restarts after a number of requests, above a memory limit and
after an idle period. Every failure raises
``HelperUnavailable``; the analyzer turns that into
``CommandAnalysisUnavailable`` so callers fail closed.

Containment: the helper is started like every server child
(``core/utils/processes.py``): windowless in its own process group, inside the
server's kill-on-close Job Object on Windows and under the process guardian on
POSIX, so it ends with the server.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from core.permissions.commands._syntax import (
    Binding,
    CallTarget,
    CallTargetKind,
    MemberCall,
    Part,
    Pipeline,
    RawCommand,
    Redirect,
    Script,
    Word,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("permissions.commands")

HELPER_SCRIPT = Path(__file__).with_name("powershell_analysis.ps1")
# Reads the helper script from the first stdin line and runs it with the same
# reader, so the requests that follow are not lost in another reader's buffer.
_BOOTSTRAP = (
    "$r=[IO.StreamReader]::new([Console]::OpenStandardInput(),[Text.UTF8Encoding]::new($false),$false);"
    "$s=$r.ReadLine();"
    "& ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s)))) $r"
)
_LINE_LIMIT = 64 * 1024 * 1024
# One helper serves every analysis in turn (a request takes milliseconds); a
# wait this long means it is overloaded or stuck and is worth a log line.
_SLOW_QUEUE_SECONDS = 1.0


class HelperUnavailable(Exception):  # noqa: N818 - names the outcome
    """The helper could not answer; the message says why, for logs and refusals."""


class HelperChannel(Protocol):
    """A started helper: one JSON line out, one JSON line back."""

    async def write_line(self, line: bytes) -> None: ...

    async def read_line(self) -> bytes: ...

    def memory_bytes(self) -> int | None: ...

    async def close(self) -> None: ...


type HelperLauncher = Callable[[], Awaitable[HelperChannel]]


@dataclass(frozen=True, slots=True)
class HelperLimits:
    """When a request gives up and when the helper starts over."""

    start_seconds: float = 20.0
    request_seconds: float = 5.0
    max_requests: int = 2000
    max_memory_bytes: int = 512 * 1024 * 1024
    idle_seconds: float = 600.0
    # After a start failed, requests fail at once for this long instead of
    # each waiting for another start.
    retry_after_seconds: float = 15.0


class PowerShellHelper:
    def __init__(self, launch: HelperLauncher, limits: HelperLimits) -> None:
        self._launch = launch
        self._limits = limits
        self._channel: HelperChannel | None = None
        self._requests = 0
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._idle: asyncio.TimerHandle | None = None
        self._closing: set[asyncio.Task[None]] = set()
        self._failed_until = 0.0
        self._failure = ""
        self._closed = False

    async def analyze(self, text: str) -> dict[str, Any]:
        """The helper's lowering of *text*; raises ``HelperUnavailable``."""
        queued = time.monotonic()
        async with self._lock:
            waited = time.monotonic() - queued
            if waited >= _SLOW_QUEUE_SECONDS:
                _LOGGER.warning(
                    "A PowerShell command analysis waited %.1f s for the helper", waited
                )
            if self._closed:
                raise HelperUnavailable("the command analysis is shutting down")
            self._cancel_idle()
            try:
                for attempt in (1, 2):
                    try:
                        return await self._request(text)
                    except HelperUnavailable as error:
                        await self._discard()
                        if attempt == 2 or time.monotonic() < self._failed_until:
                            raise
                        _LOGGER.debug(
                            "PowerShell command analysis failed (%s); retrying with a new helper",
                            error,
                        )
                raise AssertionError("unreachable")
            finally:
                self._schedule_idle()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            self._cancel_idle()
            await self._discard()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    async def _request(self, text: str) -> dict[str, Any]:
        channel = await self._channel_ready()
        self._next_id += 1
        request_id = self._next_id
        line = json.dumps(
            {"id": request_id, "script": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        )
        try:
            async with asyncio.timeout(self._limits.request_seconds):
                await channel.write_line(line.encode("utf-8") + b"\n")
                answer = await channel.read_line()
        except TimeoutError as error:
            raise HelperUnavailable(
                "the PowerShell command analysis did not answer in time"
            ) from error
        except (OSError, ValueError, asyncio.LimitOverrunError) as error:
            raise HelperUnavailable("the PowerShell command analysis stopped") from error
        if not answer:
            raise HelperUnavailable("the PowerShell command analysis stopped")
        try:
            response = json.loads(answer)
        except ValueError as error:
            raise HelperUnavailable(
                "the PowerShell command analysis answered in an unknown form"
            ) from error
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise HelperUnavailable("the PowerShell command analysis answered in an unknown form")
        if not response.get("ok"):
            raise HelperUnavailable(
                f"the PowerShell command analysis failed: {response.get('error')}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise HelperUnavailable("the PowerShell command analysis answered in an unknown form")
        self._requests += 1
        await self._retire_if_due(channel)
        return result

    async def _channel_ready(self) -> HelperChannel:
        if self._channel is not None:
            return self._channel
        if time.monotonic() < self._failed_until:
            raise HelperUnavailable(self._failure)
        try:
            async with asyncio.timeout(self._limits.start_seconds):
                channel = await self._launch()
                try:
                    ready = await channel.read_line()
                    if not ready or not json.loads(ready).get("ready"):
                        raise HelperUnavailable("the PowerShell command analysis did not start")
                except BaseException:
                    await channel.close()
                    raise
        except (TimeoutError, OSError, ValueError, HelperUnavailable) as error:
            self._failure = (
                str(error)
                if isinstance(error, HelperUnavailable)
                else "PowerShell could not be started for the command analysis"
            )
            self._failed_until = time.monotonic() + self._limits.retry_after_seconds
            _LOGGER.warning("The PowerShell command analysis could not start: %s", self._failure)
            raise HelperUnavailable(self._failure) from error
        self._channel = channel
        self._requests = 0
        _LOGGER.debug("PowerShell command analysis started")
        return channel

    async def _retire_if_due(self, channel: HelperChannel) -> None:
        reason = None
        if self._requests >= self._limits.max_requests:
            reason = f"after {self._requests} requests"
        else:
            memory = channel.memory_bytes()
            if memory is not None and memory > self._limits.max_memory_bytes:
                reason = f"at {memory // (1024 * 1024)} MiB"
        if reason is not None:
            _LOGGER.debug("Restarting the PowerShell command analysis %s", reason)
            await self._discard()

    async def _discard(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            await channel.close()

    def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _schedule_idle(self) -> None:
        if self._channel is None or self._closed:
            return
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(self._limits.idle_seconds, self._idle_expired)

    def _idle_expired(self) -> None:
        self._idle = None
        if self._lock.locked() or self._channel is None:
            return
        channel, self._channel = self._channel, None
        task = asyncio.get_running_loop().create_task(channel.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)


class _ProcessChannel:
    """A real helper process on pipes."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._psutil: Any = None

    async def write_line(self, line: bytes) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise OSError("the helper has no input")
        stdin.write(line)
        await stdin.drain()

    async def read_line(self) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            raise OSError("the helper has no output")
        return await stdout.readline()

    def memory_bytes(self) -> int | None:
        try:
            if self._psutil is None:
                import psutil  # type: ignore[import-untyped]

                self._psutil = psutil.Process(self._process.pid)
            return int(self._psutil.memory_info().rss)
        except Exception:
            return None

    async def close(self) -> None:
        from core.utils.processes import kill_process_tree_async, wait_for_exit

        if self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        if self._process.returncode is None:
            try:
                await kill_process_tree_async(self._process)
            except OSError, ProcessLookupError:
                with contextlib.suppress(OSError, ProcessLookupError):
                    self._process.kill()
        with contextlib.suppress(OSError):
            async with asyncio.timeout(5):
                await wait_for_exit(self._process)


def process_launcher(executable: Path) -> HelperLauncher:
    """Start *executable* (``pwsh`` or ``powershell``) with the helper script."""

    async def launch() -> HelperChannel:
        from core.utils.processes import (
            create_subprocess_exec,
            guarded_process_launch,
            subprocess_creation_flags,
        )

        script = base64.b64encode(HELPER_SCRIPT.read_bytes()) + b"\n"
        argv = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _BOOTSTRAP]
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            # A folder users cannot plant a library in, as for the warm shell.
            options["cwd"] = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")
            options["creationflags"] = subprocess_creation_flags(new_process_group=True)
            launch_argv: tuple[str, ...] = tuple(argv)
        else:
            environment = dict(os.environ)
            guarded = guarded_process_launch(argv, env=environment)
            launch_argv = guarded.argv
            options.update(
                cwd="/", env=environment, start_new_session=True, pass_fds=guarded.pass_fds
            )
        process = await create_subprocess_exec(
            *launch_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_LINE_LIMIT,
            **options,
        )
        channel = _ProcessChannel(process)
        try:
            await channel.write_line(script)
        except BaseException:
            await channel.close()
            raise
        return channel

    return launch


# Lowering ---------------------------------------------------------------------


def lower(result: dict[str, Any]) -> Script:
    """The ``Script`` of one helper answer.

    Commands, .NET method calls, expression strings and variable paths
    (``${C:/x}``) come in source order:
    a command at its start, a call at its end, after the commands whose
    output it uses.
    """
    errors = result.get("errors") or []
    if errors:
        reason = "PowerShell could not parse the command line"
        if result.get("incomplete"):
            reason = "the command line is incomplete"
        return Script(error=reason)
    script = Script(
        assigned={str(name) for name in result.get("assigned") or []},
        environment_changed=bool(result.get("environment_changed")),
    )
    pipelines: dict[int, int] = {}

    def pipeline_of(raw: object) -> Pipeline | None:
        if not isinstance(raw, dict):
            return None
        identity = pipelines.setdefault(int(raw["id"]), len(pipelines) + 1)
        return Pipeline(identity, int(raw["index"]))

    entries: list[RawCommand] = [
        _command(entry, pipeline_of(entry.get("pipeline")))
        for entry in result.get("commands") or []
    ]
    for entry in result.get("calls") or []:
        target = entry.get("target") or {}
        entries.append(
            RawCommand(
                [],
                str(entry.get("source", "")),
                offset=int(entry.get("offset", -1)),
                call=MemberCall(
                    member=None if entry.get("member") is None else str(entry["member"]),
                    static=bool(entry.get("static")),
                    target=CallTarget(
                        kind=_TARGET_KINDS.get(str(target.get("kind")), "other"),
                        type_name=_type_name(target.get("type")),
                        arguments=tuple(_word(word) for word in target.get("arguments") or []),
                        offset=None if target.get("offset") is None else int(target["offset"]),
                        feed=pipeline_of(target.get("feed")),
                    ),
                    arguments=tuple(_word(word) for word in entry.get("arguments") or []),
                ),
            )
        )
    for entry in result.get("strings") or []:
        word = _word(entry["word"])
        entries.append(
            RawCommand([], word.source, offset=int(entry.get("offset", -1)), expression=word)
        )
    for entry in result.get("items") or []:
        source = str(entry.get("source", ""))
        target = Word.literal(str(entry.get("path", "")), source=source, glob=True)
        entries.append(
            RawCommand(
                [],
                source,
                offset=int(entry.get("offset", -1)),
                item=Redirect("write" if entry.get("write") else "read", target),
            )
        )
    entries.sort(key=lambda raw: raw.offset)
    script.commands = entries
    return script


def _command(entry: dict[str, Any], pipeline: Pipeline | None) -> RawCommand:
    redirects = [
        Redirect("write", _word(redirect["target"])) for redirect in entry.get("redirects") or []
    ]
    offset = int(entry.get("offset", -1))
    if entry.get("expression"):
        return RawCommand([], str(entry.get("source", "")), redirects, pipeline, offset=offset)
    binding = None
    if isinstance(entry.get("binding"), dict):
        raw = entry["binding"]
        binding = Binding(
            bound={
                str(name): (None if value is None else _word(value))
                for name, value in (raw.get("bound") or {}).items()
            },
            unbound=[_word(value) for value in raw.get("unbound") or []],
            errors=[str(error) for error in raw.get("errors") or []],
        )
    return RawCommand(
        [_word(entry["name"]), *(_word(word) for word in entry.get("words") or [])],
        str(entry.get("source", "")),
        redirects,
        pipeline,
        invocation=str(entry.get("invocation") or ""),
        resolved=entry.get("resolved") or None,
        binding=binding,
        script_function=bool(entry.get("script_function")),
        stop_parsing=bool(entry.get("stop_parsing")),
        offset=offset,
    )


def _type_name(name: object) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    return name.lower().removeprefix("system.")


_TARGET_KINDS: dict[str, CallTargetKind] = {
    "type": "type",
    "new": "new",
    "command": "command",
    "item": "item",
    "text": "text",
}
_PART_KINDS = {"s": "text", "v": "variable", "d": "dynamic"}


def _word(raw: dict[str, Any]) -> Word:
    parts = tuple(
        Part(_PART_KINDS.get(str(part.get("k")), "dynamic"), str(part.get("v", "")))  # type: ignore[arg-type]
        for part in raw.get("parts") or []
    )
    items = raw.get("items")
    argument = raw.get("argument")
    return Word(
        parts,
        str(raw.get("source", "")),
        glob=True,
        tilde=True,
        items=tuple(_word(item) for item in items) if isinstance(items, list) else None,
        block=bool(raw.get("block")),
        splat=bool(raw.get("splat")),
        parameter=raw.get("parameter"),
        argument=_word(argument) if isinstance(argument, dict) else None,
    )
