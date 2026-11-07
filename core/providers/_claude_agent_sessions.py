"""Runtime-owned native Claude sessions; canonical vBot history stays authoritative."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from core.providers._claude_agent_sdk import (
    SDK_POOL,
    SUBSCRIPTION_REQUIRED_MESSAGE,
    PendingTools,
    abort_client,
    executable,
    load_sdk,
    login_info,
    options,
)
from core.providers._claude_agent_wire import (
    JsonObject,
    StreamState,
    history,
    kind,
    prompt_content,
    record,
)
from core.providers.errors import ProviderAuthError, ProviderError
from core.storage import TemporaryFileLease, TemporaryFileManager
from core.utils.logging import get_logger
from core.utils.owner_only import restrict_to_owner
from core.utils.workers import finish_despite_cancel, settle_before_cancelling

IDLE_SECONDS = 300.0
STARTUP_SECONDS = 30.0
CONTEXT_DISCOVERY_SECONDS = 60.0
SHUTDOWN_SECONDS = 10.0
MAX_SESSIONS = 16
CAPACITY_WAIT_SECONDS = 30.0
#: The usage probe's bound: start, one minimal Model response, and its result.
USAGE_PROBE_SECONDS = 60.0
#: Claude Code's moving alias for its smallest Model; it resolves to the current one.
USAGE_PROBE_MODEL = "haiku"

_LOGGER = get_logger("providers.claude_agent")


class ClaudeAgentBusyError(ProviderError):
    """The native process budget is occupied by work that cannot be evicted."""

    def __init__(self) -> None:
        super().__init__(
            "All Claude Agent process slots are busy. Try again shortly.", retryable=True
        )


@dataclass(frozen=True)
class Conversation:
    project_id: str | None
    agent_id: str
    session_id: str
    generation_id: str | None
    connection_id: str


class NativeSession:
    """One SDK client, connected and disconnected inside its single owner task."""

    def __init__(
        self,
        client_options: Any,
        pending: PendingTools,
        sdk: Any,
        *,
        temporary_files: TemporaryFileManager | None = None,
        observe_usage: Callable[[JsonObject], None] | None = None,
    ) -> None:
        self._options = client_options
        self._temporary_files = temporary_files
        self._prompt_file: TemporaryFileLease | None = None
        self._observe_usage = observe_usage
        self._process_error = getattr(sdk, "ProcessError", ())
        self._visible_output = False
        self._native_failure = False
        self.client = sdk.ClaudeSDKClient(options=client_options)
        self.pending = pending
        self.events: asyncio.Queue[JsonObject | Exception] = asyncio.Queue()
        self.ready: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self.turn_done = asyncio.Event()
        self.turn_done.set()
        self.closed = False
        self.info: JsonObject = {}
        self.version = ""
        self._stop_task: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self._end_notified = False
        self.task = asyncio.create_task(self._run(), name="claude-agent-session")
        self.task.add_done_callback(self._owner_finished)

    def _owner_finished(self, _task: asyncio.Task[None]) -> None:
        # A Task cancelled before its first instruction never enters _run's finally.
        self.closed = True
        if not self.ready.done():
            self.ready.set_exception(ProviderError("Claude Code did not start.", retryable=False))
        if not self.ready.cancelled():
            self.ready.exception()
        if not self._end_notified:
            self.pending.clear()
            self.turn_done.set()
            self.events.put_nowait(
                ProviderError("Claude Code's local process has stopped.", retryable=False)
            )
            self._end_notified = True

    async def _run(self) -> None:
        failure: Exception | None = None
        try:
            if self._temporary_files is not None:
                await SDK_POOL.run(self._prepare_files)
            await self.client.connect()
            initialized = await self.client.get_server_info()
            self.version = str((initialized or {}).get("claude_code_version", ""))
            self.info = login_info(initialized)
            self.ready.set_result(self.info)
            async for value in self.client.receive_messages():
                message = record(value)
                if kind(message) == "assistant" and message.get("error"):
                    self._native_failure = True
                if kind(message) == "assistant" and not message.get("error"):
                    self._visible_output |= bool(message.get("message", message).get("content"))
                if kind(message) == "stream_event":
                    event = message.get("event", {})
                    delta = event.get("delta", {})
                    self._visible_output |= bool(delta.get("text") or delta.get("thinking"))
                    if event.get("type") == "content_block_start":
                        block = event.get("content_block", {})
                        self._visible_output |= bool(block.get("text") or block.get("thinking"))
                if (
                    kind(message) == "rate_limit_event"
                    and self.info.get("subscription") is True
                    and self._observe_usage is not None
                ):
                    self._observe_usage(message.get("rate_limit_info", {}))
                if kind(message) == "result":
                    self.turn_done.set()
                await self.events.put(message)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            # SDK errors can contain request text or credentials; expose only this boundary.
            # Native failure diagnostics already own recovery, even when the
            # CLI dies before publishing its terminal Result.
            failure = ProviderError(
                "Claude Code's local process stopped unexpectedly.",
                retryable=(
                    isinstance(error, self._process_error)
                    and not self._visible_output
                    and not self._native_failure
                ),
            )
        finally:
            self.closed = True
            if not self.ready.done():
                self.ready.set_exception(
                    failure or ProviderError("Claude Code did not start.", retryable=False)
                )
            try:
                await self.client.disconnect()
            except Exception:
                failure = failure or ProviderError(
                    "Claude Code's local process could not close cleanly.", retryable=False
                )
            try:
                await SDK_POOL.run(self._cleanup_files)
            except OSError:
                failure = failure or ProviderError(
                    "Claude Code's private request files could not be removed.", retryable=False
                )
            self.pending.clear()
            self.turn_done.set()
            await self.events.put(
                failure
                or ProviderError("Claude Code's local process has stopped.", retryable=False)
            )
            self._end_notified = True

    def _prepare_files(self) -> None:
        assert self._temporary_files is not None
        directory = self._temporary_files.root / "providers" / "claude"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform == "win32":
            restrict_to_owner(directory)
        else:
            directory.chmod(0o700)
        self._options.cwd = str(directory)
        prompt = self._options.system_prompt
        if isinstance(prompt, str):
            lease = self._temporary_files.create("providers", ".txt")
            self._prompt_file = lease
            restrict_to_owner(lease.path)
            lease.path.write_text(prompt, encoding="utf-8", newline="\n")
            self._options.system_prompt = {"type": "file", "path": str(lease.path)}

    def _cleanup_files(self) -> None:
        lease, self._prompt_file = self._prompt_file, None
        if lease is not None:
            lease.finish()
            lease.path.unlink(missing_ok=True)

    async def start(self) -> JsonObject:
        try:
            async with asyncio.timeout(STARTUP_SECONDS):
                info = await asyncio.shield(self.ready)
        except TimeoutError as error:
            await self.stop()
            raise ProviderError(
                "Claude Code did not initialize within 30 seconds.", retryable=False
            ) from error
        if not info["connected"]:
            await self.stop()
            raise ProviderAuthError(
                info.get("message", "Run `claude auth login` on the vBot server.")
            )
        return info

    async def send(self, blocks: list[JsonObject], *, priority: bool = False) -> None:
        if self.closed:
            raise ProviderError("Claude Code's local process is closed.", retryable=False)
        if not priority:
            self.begin_step()
        self.turn_done.clear()

        async def messages() -> AsyncIterator[JsonObject]:
            message: JsonObject = {
                "type": "user",
                "message": {"role": "user", "content": blocks},
                "parent_tool_use_id": None,
            }
            if priority:
                message["priority"] = "next"
            yield message

        await self.client.query(messages())

    def begin_step(self) -> None:
        self._visible_output = False
        self._native_failure = False

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="claude-agent-stop")
        await settle_before_cancelling(self._stop_task)

    async def _stop(self) -> None:
        if not self.task.done():
            if (
                self.ready.done()
                and not self.ready.cancelled()
                and not self.closed
                and not self.turn_done.is_set()
            ):
                try:
                    async with asyncio.timeout(SHUTDOWN_SECONDS):
                        await self.client.interrupt()
                        await self.turn_done.wait()
                except Exception, asyncio.CancelledError:
                    # Closing the transport is the fallback when the native process cannot drain.
                    pass
            self._cancel_owner_once()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        if self.ready.done() and not self.ready.cancelled():
            self.ready.exception()

    def abort(self) -> None:
        try:
            abort_client(self.client)
        finally:
            self._cancel_owner_once()

    def _cancel_owner_once(self) -> None:
        # Once finally owns disconnect, cancellation would interrupt SDK cleanup itself.
        if not self.closed and not self._cancel_requested and not self.task.done():
            self._cancel_requested = True
            if not self.task.get_loop().is_closed():
                self.task.cancel()


@dataclass
class Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session: NativeSession | None = None
    signature: str = ""
    history: list[JsonObject] = field(default_factory=list)
    pending_ids: tuple[str, ...] = ()
    idle: asyncio.TimerHandle | None = None
    active: bool = False


class ClaudeAgentSessions:
    """Bound native processes across short-lived adapters, isolated by Session generation."""

    def __init__(
        self,
        *,
        native_factory: Callable[..., NativeSession] = NativeSession,
        temporary_files: TemporaryFileManager | None = None,
        executable_path: Callable[[], str | None] | None = None,
        observe_usage: Callable[[JsonObject], None] | None = None,
        on_auth_error: Callable[[], None] | None = None,
    ) -> None:
        self._entries: dict[Conversation, Entry] = {}
        self._temporary: set[NativeSession] = set()
        self._closing: set[asyncio.Task[None]] = set()
        if native_factory is NativeSession:

            def create(client_options: Any, pending: PendingTools, sdk: Any) -> NativeSession:
                if temporary_files is None:
                    raise ProviderError(
                        "Claude Agent requires a Runtime or an explicit temporary-file manager.",
                        retryable=False,
                    )
                return NativeSession(
                    client_options,
                    pending,
                    sdk,
                    temporary_files=temporary_files,
                    observe_usage=observe_usage,
                )

            self._factory = create
        else:
            self._factory = native_factory
        self._executable_path = executable_path
        self._on_auth_error = on_auth_error
        self._admission = asyncio.Lock()
        self._capacity_changed = asyncio.Event()
        self._epoch = 0
        self._closed = False

    def _executable(self) -> str:
        if self._executable_path is None:
            return executable()
        return executable(self._executable_path())

    async def probe(self) -> JsonObject:
        epoch = self._epoch
        try:
            if self._closed:
                raise ProviderError("Claude Agent is closed.", retryable=False)
            sdk = await SDK_POOL.run(load_sdk)
            path = await SDK_POOL.run(self._executable)
            if epoch != self._epoch:
                raise ProviderError("Claude Agent was closed during discovery.", retryable=False)
            pending = PendingTools([])
            await self._make_space(wait=False)
            if epoch != self._epoch:
                raise ProviderError("Claude Agent was closed during discovery.", retryable=False)
            native = self._factory(options(sdk, path), pending, sdk)
            self._temporary.add(native)
            try:
                async with asyncio.timeout(STARTUP_SECONDS):
                    info = await asyncio.shield(native.ready)
                match = re.match(r"^(\d+)\.(\d+)\.(\d+)", native.version)
                if info["connected"] and match and tuple(map(int, match.groups())) >= (2, 1, 257):
                    try:
                        async with asyncio.timeout(CONTEXT_DISCOVERY_SECONDS):
                            for model in info["models"]:
                                try:
                                    async with asyncio.timeout(5):
                                        await native.client.set_model(model["value"])
                                        usage = await native.client.get_context_usage()
                                        window = usage.get("rawMaxTokens")
                                        if (
                                            isinstance(window, int)
                                            and not isinstance(window, bool)
                                            and window > 0
                                        ):
                                            model["contextWindow"] = window
                                except Exception:
                                    # Missing optional capacity keeps the login and catalog.
                                    info["context_discovery_incomplete"] = True
                                    continue
                    except TimeoutError:
                        info["context_discovery_incomplete"] = True
                return info
            finally:
                await finish_despite_cancel(native.stop())
                self._temporary.discard(native)
                self._capacity_changed.set()
        except ClaudeAgentBusyError:
            return {
                "connected": False,
                "status": "busy",
                "message": "Try again after an active Run finishes.",
                "models": [],
                "subscription": False,
            }
        except ProviderAuthError as error:
            return {
                "connected": False,
                "status": "missing",
                "message": str(error),
                "models": [],
                "subscription": False,
            }
        except Exception:
            return {
                "connected": False,
                "status": "unavailable",
                "message": (
                    "Claude Code could not be checked. Run `claude auth login` on the vBot server."
                ),
                "models": [],
                "subscription": False,
            }

    async def probe_usage(self) -> None:
        """Send one minimal request so Claude Code reports its subscription windows.

        Claude Code reports them only beside a Model response, once per process;
        the native observation callback records them. Usage is optional, so a
        failure, a busy process budget or a non-subscription login only ends
        the probe.
        """
        epoch = self._epoch
        try:
            if self._closed:
                return
            sdk = await SDK_POOL.run(load_sdk)
            path = await SDK_POOL.run(self._executable)
            if epoch != self._epoch:
                return
            await self._make_space(wait=False)
            if epoch != self._epoch:
                return
            client_options = options(
                sdk,
                path,
                model=USAGE_PROBE_MODEL,
                system="Reply with OK.",
                controls={"max_tokens": 16, "thinking": {"type": "disabled"}},
            )
            native = self._factory(client_options, PendingTools([]), sdk)
            self._temporary.add(native)
            try:
                async with asyncio.timeout(USAGE_PROBE_SECONDS):
                    info = await native.start()
                    if info.get("subscription") is not True:
                        return
                    await native.send([{"type": "text", "text": "OK"}])
                    await native.turn_done.wait()
            finally:
                await finish_despite_cancel(native.stop())
                self._temporary.discard(native)
                self._capacity_changed.set()
        except Exception as error:
            _LOGGER.info("Claude usage probe failed (error=%s)", type(error).__name__)

    async def stream(
        self,
        *,
        conversation: Conversation | None,
        messages: list[JsonObject],
        system: str,
        model: str,
        tools: list[JsonObject],
        controls: JsonObject,
    ) -> AsyncGenerator[JsonObject]:
        if self._closed:
            raise ProviderError("Claude Agent is closed.", retryable=False)
        epoch = self._epoch
        entry = self._entries.get(conversation) if conversation is not None else None
        if entry is None:
            entry = Entry()
            if conversation is not None:
                self._entries[conversation] = entry
        elif conversation is not None:
            # Dict insertion order is the native process's least-recent-use order.
            self._entries.pop(conversation)
            self._entries[conversation] = entry
        if entry.idle:
            entry.idle.cancel()
            entry.idle = None
        async with entry.lock:
            entry.active = True
            parked = False
            complete = False
            try:
                current = await SDK_POOL.run(history, messages)
                signature = await SDK_POOL.run(
                    json.dumps, [model, system, tools, controls], ensure_ascii=False, sort_keys=True
                )
                if epoch != self._epoch:
                    raise ProviderError("Claude Agent was closed during startup.", retryable=False)
                matching = (
                    len(entry.history) <= len(current)
                    and entry.history == current[: len(entry.history)]
                )
                if entry.session and (
                    entry.session.closed or entry.signature != signature or not matching
                ):
                    await self._stop_entry(entry)
                missing = messages_without_system(messages)[len(entry.history) :]
                content = None
                if not entry.pending_ids:
                    if not missing:
                        raise ProviderError(
                            "Claude Agent received no new conversation input.", retryable=False
                        )
                    # Invalid attachments must not launch a process or alter native state.
                    content = await SDK_POOL.run(
                        prompt_content, missing, checkpoint=not entry.history
                    )
                if entry.session is None:
                    sdk = await SDK_POOL.run(load_sdk)
                    path = await SDK_POOL.run(self._executable)
                    if epoch != self._epoch:
                        raise ProviderError(
                            "Claude Agent was closed during startup.", retryable=False
                        )
                    pending = PendingTools(tools)
                    await self._make_space()
                    if epoch != self._epoch:
                        raise ProviderError(
                            "Claude Agent was closed during startup.", retryable=False
                        )
                    entry.session = self._factory(
                        options(
                            sdk,
                            path,
                            model=model,
                            system=system,
                            pending=pending,
                            controls=controls,
                        ),
                        pending,
                        sdk,
                    )
                    self._temporary.add(entry.session)
                    info = await entry.session.start()
                    if info.get("subscription") is not True:
                        raise ProviderAuthError(info.get("message", SUBSCRIPTION_REQUIRED_MESSAGE))
                    entry.signature = signature
                    entry.history = []
                    entry.pending_ids = ()
                if epoch != self._epoch:
                    raise ProviderError("Claude Agent was closed during startup.", retryable=False)
                native = entry.session
                if entry.pending_ids:
                    results = {}
                    end = 0
                    for message in missing:
                        if message.get("role") != "tool":
                            break
                        call_id = str(message.get("tool_call_id"))
                        if call_id in results:
                            raise ProviderError(
                                "Claude Agent received duplicate Tool Results.", retryable=False
                            )
                        results[call_id] = message
                        end += 1
                    if set(results) != set(entry.pending_ids):
                        # Never release a partial or mismatched batch into the native loop.
                        await self._stop_entry(entry)
                        raise ProviderError(
                            "Claude Agent's pending Tool Results do not match canonical history.",
                            retryable=False,
                        )
                    remaining = missing[end:]
                    content = (
                        await SDK_POOL.run(prompt_content, remaining, checkpoint=True)
                        if remaining
                        else None
                    )
                    release_results = await native.pending.prepare_results(
                        [results[call_id] for call_id in entry.pending_ids]
                    )
                    if epoch != self._epoch:
                        raise ProviderError(
                            "Claude Agent was closed during Tool continuation.", retryable=False
                        )
                    native.begin_step()
                    if remaining:
                        assert content is not None
                        await native.send(content, priority=True)
                    if epoch != self._epoch:
                        raise ProviderError(
                            "Claude Agent was closed during Tool continuation.", retryable=False
                        )
                    release_results()
                    entry.pending_ids = ()
                else:
                    assert content is not None
                    await native.send(content)
                state = StreamState(
                    native.pending.names, subscription=bool(native.info.get("subscription"))
                )
                while True:
                    native_message = await native.events.get()
                    if isinstance(native_message, Exception):
                        raise native_message
                    if (
                        kind(native_message) == "system"
                        and native_message.get("subtype") == "init"
                        and tools
                    ):
                        data = native_message.get("data", native_message)
                        servers = data.get("mcp_servers", [])
                        if not any(
                            server.get("name") == "vbot" and server.get("status") == "connected"
                            for server in servers
                        ):
                            raise ProviderError(
                                "Claude Code could not connect to vBot's Tool bridge.",
                                retryable=False,
                            )
                    deltas = state.consume(native_message)
                    if state.calls and state.message_stopped:
                        # AssistantMessages are content blocks, not API-message boundaries.
                        # Settle every sibling Call and final Usage before yielding finish.
                        if deltas[-1]["reason"] == "tool_calls":
                            for call in state.calls:
                                native.pending.expect(str(call["id"]), call["name"])
                            entry.pending_ids = tuple(str(call["id"]) for call in state.calls)
                            entry.history = current + [state.assistant()]
                            parked = True
                            complete = True
                        for delta in deltas:
                            yield delta
                        return
                    if kind(native_message) == "result":
                        # Consumers can close at finish: publish settled state before yielding it.
                        entry.history = current + [state.assistant()]
                        complete = True
                    for delta in deltas:
                        yield delta
                    if kind(native_message) == "result":
                        return
            except ProviderAuthError:
                if self._on_auth_error is not None:
                    self._on_auth_error()
                raise
            finally:
                entry.active = False
                self._capacity_changed.set()
                if not complete or conversation is None:
                    await finish_despite_cancel(self._stop_entry(entry))
                    if conversation is not None and self._entries.get(conversation) is entry:
                        self._entries.pop(conversation)
                elif not parked and conversation is not None:
                    self._arm_idle(conversation, entry)

    async def release(self, conversation: Conversation) -> None:
        entry = self._entries.get(conversation)
        if entry is None or entry.active:
            return
        if entry.pending_ids:
            await self._remove(conversation)
        else:
            self._arm_idle(conversation, entry)

    def _arm_idle(self, conversation: Conversation, entry: Entry) -> None:
        if entry.idle:
            entry.idle.cancel()

        def expire() -> None:
            timer = entry.idle

            async def remove_idle() -> None:
                async with entry.lock:
                    if (
                        entry.idle is not timer
                        or entry.active
                        or entry.pending_ids
                        or self._entries.get(conversation) is not entry
                    ):
                        return
                    self._entries.pop(conversation)
                    await self._stop_entry(entry)

            task = asyncio.create_task(remove_idle(), name="claude-agent-idle")
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

        entry.idle = asyncio.get_running_loop().call_later(IDLE_SECONDS, expire)

    async def _make_space(self, *, wait: bool = True) -> None:
        if not wait and self._admission.locked():
            raise ClaudeAgentBusyError()
        try:
            async with asyncio.timeout(CAPACITY_WAIT_SECONDS):
                async with self._admission:
                    while True:
                        if self._closed:
                            raise ProviderError("Claude Agent is closed.", retryable=False)
                        if len(self._temporary) < MAX_SESSIONS:
                            return
                        candidate = next(
                            (
                                key
                                for key, entry in self._entries.items()
                                if entry.session is not None
                                and not entry.active
                                and (wait or not entry.pending_ids)
                            ),
                            None,
                        )
                        if candidate is not None:
                            await self._remove(candidate)
                            continue
                        if not wait:
                            raise ClaudeAgentBusyError()
                        self._capacity_changed.clear()
                        await self._capacity_changed.wait()
        except TimeoutError as error:
            raise ClaudeAgentBusyError() from error

    async def _stop_entry(self, entry: Entry) -> None:
        if entry.idle:
            entry.idle.cancel()
            entry.idle = None
        native, entry.session = entry.session, None
        if native:
            try:
                await native.stop()
            finally:
                self._temporary.discard(native)
                self._capacity_changed.set()
        entry.history = []
        entry.pending_ids = ()

    async def _remove(self, conversation: Conversation) -> None:
        entry = self._entries.pop(conversation, None)
        if entry:
            if entry.active and entry.session:
                entry.session.abort()
            async with entry.lock:
                await self._stop_entry(entry)

    async def close_sessions(self, sessions: set[tuple[str, str]]) -> None:
        for key in list(self._entries):
            if (key.agent_id, key.session_id) in sessions:
                await self._remove(key)

    async def aclose(self) -> None:
        self._closed = True
        self._epoch += 1
        self._capacity_changed.set()

        async def close_all() -> None:
            failures: list[BaseException] = []
            results = await asyncio.gather(
                *(self._remove(key) for key in list(self._entries)), return_exceptions=True
            )
            failures.extend(result for result in results if isinstance(result, BaseException))

            async def close_native(native: NativeSession) -> None:
                try:
                    await native.stop()
                finally:
                    self._temporary.discard(native)
                    self._capacity_changed.set()

            remaining = [close_native(native) for native in list(self._temporary)]
            results = await asyncio.gather(*remaining, *list(self._closing), return_exceptions=True)
            failures.extend(result for result in results if isinstance(result, BaseException))
            if len(failures) == 1:
                raise failures[0]
            if failures:
                raise BaseExceptionGroup("Claude Agent processes could not all close.", failures)

        await settle_before_cancelling(close_all())

    def abort(self) -> None:
        self._closed = True
        self._epoch += 1
        self._capacity_changed.set()
        for entry in self._entries.values():
            if entry.idle:
                entry.idle.cancel()
        for native in self._temporary:
            native.abort()
        self._entries.clear()


def messages_without_system(messages: list[JsonObject]) -> list[JsonObject]:
    return [message for message in messages if message.get("role") != "system"]
