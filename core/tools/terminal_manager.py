"""Session-scoped interactive PTY/ConPTY lifecycle and activity management."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from core.event_stream import ReplayEventStream
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.tools.terminal_backend import (
    TerminalAdapter,
    TerminalAdapterFactory,
    TerminalRenderer,
    default_terminal_argv,
    spawn_terminal_adapter,
    terminate_process_tree,
)
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.terminal_manager")

TERMINAL_DEFAULT_COLUMNS = 120
TERMINAL_DEFAULT_ROWS = 32
TERMINAL_MIN_COLUMNS = 40
TERMINAL_MAX_COLUMNS = 240
TERMINAL_MIN_ROWS = 10
TERMINAL_MAX_ROWS = 100
TERMINAL_SCROLLBACK_LINES = 2_000
TERMINAL_STATUS_DEFAULT_LINES = 30
TERMINAL_STATUS_MAX_LINES = 100
TERMINAL_MAX_LIVE_PER_SESSION = 4
TERMINAL_MAX_LIVE_GLOBAL = 32
TERMINAL_SWEEP_INTERVAL_SECONDS = 60.0
TERMINAL_FINISHED_TTL = timedelta(minutes=30)
TERMINAL_NOTICE_MESSAGE_CAP_CHARS = 16_000
TERMINAL_CURSOR_VERSION = 1
TERMINAL_TEMPORARY_CATEGORY = "terminals"
TERMINAL_INITIAL_INPUT_QUIET_SECONDS = 0.5
TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS = 15.0
TERMINAL_ACTIVITY_QUIET_SECONDS = 2.0
TERMINAL_INPUT_KEY_DELAY_SECONDS = 0.1
TERMINAL_STREAM_RETENTION_EVENTS = 4_096
TERMINAL_STREAM_SUBSCRIBER_QUEUE_EVENTS = 512
TERMINAL_INPUT_MAX_CHARS = 65_536
TERMINAL_INPUT_KEY_SEQUENCES = {
    "enter": "\r",
    "escape": "\x1b",
    "tab": "\t",
    "shift_tab": "\x1b[Z",
    "backspace": "\x7f",
    "insert": "\x1b[2~",
    "delete": "\x1b[3~",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "page_up": "\x1b[5~",
    "page_down": "\x1b[6~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
    **{f"ctrl_{chr(code + 96)}": chr(code) for code in range(1, 27)},
}
TerminalState = Literal[
    "starting",
    "ready",
    "working",
    "exited",
    "error",
]
AttentionKind = Literal["output_settled", "exited", "error"]
TerminalStreamEvent = dict[str, Any]
TerminalChangedCallback = Callable[[str], None]


def _new_terminal_stream() -> ReplayEventStream[TerminalStreamEvent]:
    return ReplayEventStream(
        event_retention_limit=TERMINAL_STREAM_RETENTION_EVENTS,
        subscriber_queue_limit=TERMINAL_STREAM_SUBSCRIBER_QUEUE_EVENTS,
        sequence_of=lambda event: int(event.get("sequence", 0)),
        terminal_when=lambda event: (
            event.get("type") == "terminal_state"
            and event.get("terminal", {}).get("state") in {"exited", "error"}
        ),
        on_lagged=lambda: _LOGGER.warning("Evicted lagging Terminal stream subscriber"),
    )


class TerminalManagerError(VBotError):
    """Base class for expected Terminal Manager failures."""


class TerminalNotFoundError(TerminalManagerError):
    """Raised when a Terminal Session is missing or belongs to another Session."""


class TerminalClosedError(TerminalManagerError):
    """Raised when input or resize targets a closed Terminal Session."""


class TerminalCapacityError(TerminalManagerError):
    """Raised when a live Terminal Session capacity limit is reached."""


class TerminalLaunchError(TerminalManagerError):
    """Raised when the host cannot start a requested terminal process."""


class TerminalStaleScreenError(TerminalManagerError):
    """Raised when input was based on an obsolete rendered screen."""


class TerminalCursorError(TerminalManagerError):
    """Raised when a scrollback cursor is malformed or no longer available."""


@dataclass(frozen=True, slots=True)
class TerminalOwner:
    """Exact vBot Session authority for one Terminal Session."""

    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(slots=True)
class TerminalAttention:
    """One program-agnostic Agent-attention boundary for a Terminal Session."""

    revision: int
    kind: AttentionKind
    notice_id: str
    summary: str
    details: dict[str, Any]
    created_at: datetime
    delivered: bool = False


@dataclass(slots=True)
class TerminalSession:
    """In-memory state for one interactive terminal process."""

    terminal_id: str
    owner: TerminalOwner | None
    adapter: TerminalAdapter
    renderer: TerminalRenderer
    command: str
    arguments: tuple[str, ...]
    cwd: Path
    state: TerminalState
    started_at: datetime
    origin_run_id: str | None
    activity_origin_run_id: str | None
    log_path: Path | None
    log_handle: TextIO | None
    log_lease: TemporaryFileLease | None
    exit_code: int | None = None
    finished_at: datetime | None = None
    attention_revision: int = 0
    acknowledged_attention_revision: int = 0
    attention: TerminalAttention | None = None
    activity_generation: int = 0
    notify_on_settle: bool = False
    suppress_exit_attention: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    output_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    attention_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    initial_input_task: asyncio.Task[None] | None = field(default=None, repr=False)
    settle_task: asyncio.Task[None] | None = field(default=None, repr=False)
    notification_task: asyncio.Task[None] | None = field(default=None, repr=False)
    stream_sequence: int = 0
    stream: ReplayEventStream[TerminalStreamEvent] = field(
        default_factory=_new_terminal_stream, repr=False
    )


class TerminalManager:
    """Own Agent and manually started interactive terminal processes."""

    def __init__(
        self,
        trigger_service: Any | None = None,
        *,
        temporary_files: TemporaryFileManager | None = None,
        adapter_factory: TerminalAdapterFactory | None = None,
        scrollback_lines: int = TERMINAL_SCROLLBACK_LINES,
        finished_session_ttl: timedelta = TERMINAL_FINISHED_TTL,
        sweep_interval_seconds: float = TERMINAL_SWEEP_INTERVAL_SECONDS,
        activity_quiet_seconds: float = TERMINAL_ACTIVITY_QUIET_SECONDS,
    ) -> None:
        if scrollback_lines < 1:
            raise ValueError("Terminal scrollback cap must be positive")
        if finished_session_ttl <= timedelta(0):
            raise ValueError("Terminal finished-session TTL must be positive")
        if sweep_interval_seconds <= 0:
            raise ValueError("Terminal sweep interval must be positive")
        if activity_quiet_seconds <= 0:
            raise ValueError("Terminal activity quiet period must be positive")
        self._trigger_service = trigger_service
        self._temporary_files = temporary_files
        self._adapter_factory = adapter_factory or spawn_terminal_adapter
        self._scrollback_lines = scrollback_lines
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._activity_quiet_seconds = activity_quiet_seconds
        self._sessions: dict[str, TerminalSession] = {}
        self._changed_callbacks: list[TerminalChangedCallback] = []
        self._cursor_secret = os.urandom(32)
        self._sweeper_task: asyncio.Task[None] | None = None

    def add_changed_callback(self, callback: TerminalChangedCallback) -> Callable[[], None]:
        """Notify transport edges when operator-visible Terminal state changes."""
        self._changed_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._changed_callbacks:
                self._changed_callbacks.remove(callback)

        return unsubscribe

    def start(self) -> None:
        """Start bounded retention cleanup when an event loop is available."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop(), name="terminal-manager-sweep")

    def stop(self) -> None:
        """Synchronously stop every child and cancel background tasks."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        for session in list(self._sessions.values()):
            session.suppress_exit_attention = True
            self._cancel_delivery(session)
            if session.state not in {"exited", "error"}:
                terminate_process_tree(session.adapter)
            for task in (session.reader_task, session.initial_input_task, session.settle_task):
                if task is not None and not task.done():
                    task.cancel()
            self._finish_files(session)

    async def aclose(self) -> None:
        """Stop all children and await reader, event, notification, and sweep tasks."""
        sweeper = self._sweeper_task
        self.stop()
        tasks: list[asyncio.Task[Any]] = []
        if sweeper is not None and not sweeper.done():
            tasks.append(sweeper)
        for session in self._sessions.values():
            for task in (
                session.reader_task,
                session.initial_input_task,
                session.settle_task,
                session.notification_task,
            ):
                if task is not None and not task.done():
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def spawn(
        self,
        owner: TerminalOwner,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        columns: int,
        rows: int,
        origin_run_id: str,
        initial_text: str | None = None,
    ) -> TerminalSession:
        """Start one Agent-owned program behind PTY/ConPTY."""
        _validate_owner(owner)
        return await self._spawn(
            owner,
            argv,
            cwd=cwd,
            env=env,
            columns=columns,
            rows=rows,
            origin_run_id=origin_run_id,
            initial_text=initial_text,
        )

    async def spawn_for_operator(
        self,
        *,
        command: str | None,
        arguments: Sequence[str],
        cwd: Path | None,
        columns: int = TERMINAL_DEFAULT_COLUMNS,
        rows: int = TERMINAL_DEFAULT_ROWS,
    ) -> dict[str, Any]:
        """Start one manual Terminal Session through the ordinary PTY path."""
        argv = [command] if command is not None else default_terminal_argv()
        argv.extend(arguments)
        session = await self._spawn(
            None,
            argv,
            cwd=cwd or Path.home(),
            env=None,
            columns=columns,
            rows=rows,
            origin_run_id=None,
        )
        return self._operator_summary(session)

    async def _spawn(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        columns: int,
        rows: int,
        origin_run_id: str | None,
        initial_text: str | None = None,
    ) -> TerminalSession:
        """Start one unmodified program behind PTY/ConPTY."""
        if not argv or not argv[0]:
            raise ValueError("Terminal command must not be empty")
        if any(not isinstance(token, str) for token in argv):
            raise ValueError("Terminal command and arguments must be strings")
        _validate_dimensions(columns, rows)
        if not cwd.is_dir():
            raise ValueError(f"Terminal workdir is not a directory: {cwd}")
        self._enforce_capacity(owner)

        terminal_id = uuid.uuid4().hex
        log_path: Path | None = None
        log_handle: TextIO | None = None
        log_lease: TemporaryFileLease | None = None
        process_env = dict(os.environ)
        if env is not None:
            process_env.update(env)
        process_env.setdefault("TERM", "xterm-256color")

        try:
            log_path, log_handle, log_lease = self._open_raw_log()
            adapter = await asyncio.to_thread(
                self._adapter_factory,
                list(argv),
                cwd,
                process_env,
                rows,
                columns,
            )
        except Exception as error:
            if log_handle is not None:
                log_handle.close()
            if log_lease is not None:
                log_lease.finish()
            raise TerminalLaunchError(f"Terminal process could not be started: {error}") from error

        session = TerminalSession(
            terminal_id=terminal_id,
            owner=owner,
            adapter=adapter,
            renderer=TerminalRenderer(columns, rows, scrollback_lines=self._scrollback_lines),
            command=argv[0],
            arguments=tuple(argv[1:]),
            cwd=cwd,
            state="starting" if initial_text is not None else "ready",
            started_at=_utc_now(),
            origin_run_id=origin_run_id,
            activity_origin_run_id=None,
            log_path=log_path,
            log_handle=log_handle,
            log_lease=log_lease,
        )
        self._sessions[terminal_id] = session
        session.reader_task = asyncio.create_task(
            self._read_terminal(session), name=f"terminal:{terminal_id}:reader"
        )
        session.reader_task.add_done_callback(
            lambda task: _log_background_task_result(
                task, f"Terminal reader failed for terminal={terminal_id}"
            )
        )
        if initial_text is not None and origin_run_id is not None:
            session.initial_input_task = asyncio.create_task(
                self._send_initial_input_when_ready(
                    session, initial_text, origin_run_id=origin_run_id
                ),
                name=f"terminal:{terminal_id}:initial-input",
            )
            session.initial_input_task.add_done_callback(
                lambda task: _log_background_task_result(
                    task, f"Terminal initial input failed for terminal={terminal_id}"
                )
            )
        self._publish_state(session)
        return session

    def list_sessions(self, owner: TerminalOwner) -> list[TerminalSession]:
        """Return all retained Terminal Sessions owned by one vBot Session."""
        return sorted(
            (session for session in self._sessions.values() if session.owner == owner),
            key=lambda session: session.started_at,
        )

    def get_session(self, terminal_id: str, owner: TerminalOwner) -> TerminalSession:
        """Return an owned Terminal Session without revealing cross-Session ids."""
        session = self._sessions.get(terminal_id)
        if session is None or session.owner != owner:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return session

    def list_for_operator(self) -> list[dict[str, Any]]:
        """Return live and temporarily retained Terminal Sessions for inspection."""
        sessions = sorted(
            self._sessions.values(),
            key=lambda session: (
                session.finished_at is None,
                session.finished_at or session.started_at,
            ),
            reverse=True,
        )
        return [self._operator_summary(session) for session in sessions]

    async def watch_for_operator(
        self, terminal_id: str
    ) -> AsyncGenerator[TerminalStreamEvent, None]:
        """Yield an authoritative VT snapshot followed by sequenced live events."""
        session = self._get_for_operator(terminal_id)
        async with session.lock:
            after_sequence = session.stream_sequence
            ready: TerminalStreamEvent = {
                "type": "terminal_ready",
                "sequence": after_sequence,
                "terminal": self._operator_summary(session),
                "ansi": session.renderer.ansi_snapshot(),
            }
        yield ready
        if session.state in {"exited", "error"}:
            return
        async with contextlib.aclosing(
            session.stream.subscribe(after_sequence=after_sequence)
        ) as events:
            async for event in events:
                yield event

    async def send_operator_input(self, terminal_id: str, data: str) -> dict[str, Any]:
        """Write exact user-controlled terminal bytes through the existing PTY."""
        if not isinstance(data, str) or not data:
            raise ValueError("Terminal input must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(
                f"Terminal input must not exceed {TERMINAL_INPUT_MAX_CHARS} characters"
            )
        session = self._get_for_operator(terminal_id)
        initial_task = session.initial_input_task
        if initial_task is not None and not initial_task.done():
            initial_task.cancel()
        async with session.lock:
            self._require_live(session)
            state_changed = session.state != "working"
            session.state = "working"
            await asyncio.to_thread(session.adapter.write, data)
            self._schedule_settle(session, notify=False)
            session.output_event.set()
            if state_changed:
                self._publish_state(session)
            return self._operator_summary(session)

    async def resize_for_operator(
        self, terminal_id: str, *, columns: int, rows: int
    ) -> dict[str, Any]:
        """Resize an operator-selected Terminal Session."""
        session = self._get_for_operator(terminal_id)
        await self._resize_session(session, columns=columns, rows=rows)
        return self._operator_summary(session)

    async def kill_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Explicitly stop an operator-selected Terminal Session."""
        session = self._get_for_operator(terminal_id)
        await self._terminate_session(session, suppress_attention=True)
        return self._operator_summary(session)

    def forget_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Remove one finished Terminal Session from the retained operator catalog."""
        session = self._get_for_operator(terminal_id)
        if session.state not in {"exited", "error"} or session.finished_at is None:
            raise ValueError("A running Terminal Session must be stopped before removal")
        summary = self._operator_summary(session)
        self._sessions.pop(terminal_id)
        self._cancel_delivery(session)
        self._finish_files(session)
        self._notify_changed(terminal_id)
        return summary

    async def snapshot(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        lines: int = TERMINAL_STATUS_DEFAULT_LINES,
        before: int | None = None,
    ) -> dict[str, Any]:
        """Return one bounded rendered status page."""
        if not 1 <= lines <= TERMINAL_STATUS_MAX_LINES:
            raise ValueError(f"lines must be between 1 and {TERMINAL_STATUS_MAX_LINES}")
        session = self.get_session(terminal_id, owner)
        async with session.lock:
            try:
                scrollback = session.renderer.page(before=before, limit=lines)
            except ValueError as error:
                raise TerminalCursorError(str(error)) from error
            return self._snapshot_data(session, scrollback)

    async def wait_for_attention(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        after_revision: int,
        timeout_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        """Wait for a newer attention revision without owning the child lifetime."""
        session = self.get_session(terminal_id, owner)
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        timed_out = False
        while session.attention_revision <= after_revision and session.state not in {
            "exited",
            "error",
        }:
            session.attention_event.clear()
            if session.attention_revision > after_revision:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(session.attention_event.wait(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                break
        return await self.snapshot(terminal_id, owner), timed_out

    async def send_input(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        text: str | None,
        key: str | None,
        enter: bool,
        expected_screen_revision: int | None,
        origin_run_id: str,
        data: str | None = None,
    ) -> dict[str, Any]:
        """Write exact data or named terminal input and track generic PTY activity."""
        session = self.get_session(terminal_id, owner)
        chunks = _input_chunks(data=data, text=text, key=key, enter=enter)
        initial_task = session.initial_input_task
        if (
            initial_task is not None
            and initial_task is not asyncio.current_task()
            and not initial_task.done()
        ):
            initial_task.cancel()
        async with session.lock:
            self._require_live(session)
            if (
                expected_screen_revision is not None
                and expected_screen_revision != session.renderer.revision
            ):
                raise TerminalStaleScreenError(
                    "Terminal screen changed; inspect status before sending this input"
                )
            prior_state = session.state
            prior_attention_revision = session.attention_revision
            session.activity_origin_run_id = origin_run_id
            session.state = "working"
            for index, chunk in enumerate(chunks):
                if index:
                    await asyncio.sleep(TERMINAL_INPUT_KEY_DELAY_SECONDS)
                await asyncio.to_thread(session.adapter.write, chunk)
            self._schedule_settle(session, notify=True)
            session.output_event.set()
            if prior_state != "working":
                self._publish_state(session)
            return {
                "terminal_id": terminal_id,
                "state": session.state,
                "characters_sent": sum(len(chunk) for chunk in chunks),
                "key": key,
                "enter": enter,
                "superseded_attention_revision": (
                    prior_attention_revision if session.attention is not None else None
                ),
                "screen_revision": session.renderer.revision,
            }

    async def _send_initial_input_when_ready(
        self, session: TerminalSession, text: str, *, origin_run_id: str
    ) -> None:
        """Wait until the TUI has initialized before sending its first task."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS
        last_revision = -1
        quiet_since: float | None = None
        try:
            while session.state not in {"exited", "error"}:
                now = loop.time()
                if now >= deadline:
                    break
                async with session.lock:
                    revision = session.renderer.revision
                    has_screen = bool(session.renderer.screen_text())
                if has_screen and revision != last_revision:
                    last_revision = revision
                    quiet_since = now
                elif (
                    has_screen
                    and quiet_since is not None
                    and now - quiet_since >= TERMINAL_INITIAL_INPUT_QUIET_SECONDS
                ):
                    break
                session.output_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(session.output_event.wait(), timeout=0.1)
            if session.state in {"exited", "error"}:
                return
            owner = session.owner
            if owner is None:
                return
            await self.send_input(
                session.terminal_id,
                owner,
                data=None,
                text=text,
                key=None,
                enter=True,
                expected_screen_revision=None,
                origin_run_id=origin_run_id,
            )
        except asyncio.CancelledError:
            return

    async def resize(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        columns: int,
        rows: int,
    ) -> dict[str, Any]:
        """Resize both the host PTY/ConPTY and rendered screen."""
        session = self.get_session(terminal_id, owner)
        return await self._resize_session(session, columns=columns, rows=rows)

    async def _resize_session(
        self,
        session: TerminalSession,
        *,
        columns: int,
        rows: int,
    ) -> dict[str, Any]:
        """Resize one already-authorized Terminal Session."""
        _validate_dimensions(columns, rows)
        async with session.lock:
            self._require_live(session)
            await asyncio.to_thread(session.adapter.resize, rows, columns)
            session.renderer.resize(columns, rows)
            self._publish_state(session)
            return {
                "terminal_id": session.terminal_id,
                "state": session.state,
                "columns": columns,
                "rows": rows,
                "screen_revision": session.renderer.revision,
            }

    async def kill(self, terminal_id: str, owner: TerminalOwner) -> dict[str, Any]:
        """Explicitly terminate one Terminal Session without an automatic exit wakeup."""
        session = self.get_session(terminal_id, owner)
        await self._terminate_session(session, suppress_attention=True)
        return await self.snapshot(terminal_id, owner)

    async def close_scope(self, owner: TerminalOwner) -> None:
        """Terminate every Terminal Session owned by one removed vBot Session."""
        sessions = [session for session in self._sessions.values() if session.owner == owner]
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        """Terminate Terminal Sessions under an Agent/Project owner being removed."""
        sessions = [
            session
            for session in self._sessions.values()
            if session.owner is not None
            and session.owner.agent_id == agent_id
            and session.owner.project_id == project_id
        ]
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_project_scope(self, project_id: str) -> None:
        """Terminate every Terminal Session under a removed Project."""
        sessions = [
            session
            for session in self._sessions.values()
            if session.owner is not None and session.owner.project_id == project_id
        ]
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    def transfer_scope(self, source: TerminalOwner, target: TerminalOwner) -> int:
        """Transfer all Terminal Sessions after a successful `/agent` Session move."""
        transferred = 0
        for session in self._sessions.values():
            if session.owner == source:
                attention = session.attention
                pending_delivery = (
                    attention is not None
                    and session.notification_task is not None
                    and not session.notification_task.done()
                )
                if pending_delivery:
                    self._cancel_delivery(session)
                session.owner = target
                self._publish_state(session)
                if pending_delivery and attention is not None:
                    self._schedule_attention_delivery(session, attention)
                transferred += 1
        return transferred

    def acknowledge_attention(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        revision: int,
    ) -> None:
        """Cancel equivalent automatic delivery after a manual result is durable."""
        session = self.get_session(terminal_id, owner)
        attention = session.attention
        if attention is None or attention.revision != revision:
            return
        session.acknowledged_attention_revision = max(
            session.acknowledged_attention_revision, revision
        )
        self._cancel_delivery(session)

    def encode_cursor(self, terminal_id: str, before: int) -> str:
        """Encode one process-local signed scrollback continuation."""
        payload = json.dumps(
            {"v": TERMINAL_CURSOR_VERSION, "terminal_id": terminal_id, "before": before},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    def decode_cursor(self, cursor: str, terminal_id: str) -> int:
        """Validate one signed cursor and return its exclusive line boundary."""
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload, signature = decoded[:-32], decoded[-32:]
            expected = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise TerminalCursorError("Terminal scrollback cursor is invalid") from error
        if (
            not isinstance(data, dict)
            or data.get("v") != TERMINAL_CURSOR_VERSION
            or data.get("terminal_id") != terminal_id
            or isinstance(data.get("before"), bool)
            or not isinstance(data.get("before"), int)
            or data["before"] < 1
        ):
            raise TerminalCursorError("Terminal scrollback cursor is invalid")
        return cast(int, data["before"])

    async def sweep_finished(self) -> None:
        """Forget terminal metadata after the bounded inspection window."""
        cutoff = _utc_now() - self._finished_session_ttl
        expired = [
            terminal_id
            for terminal_id, session in self._sessions.items()
            if session.finished_at is not None and session.finished_at < cutoff
        ]
        for terminal_id in expired:
            session = self._sessions.pop(terminal_id)
            self._cancel_delivery(session)
            self._finish_files(session)
            self._notify_changed(terminal_id)

    async def _read_terminal(self, session: TerminalSession) -> None:
        error: BaseException | None = None
        try:
            while True:
                text = await asyncio.to_thread(session.adapter.read, 4096)
                if not text:
                    if not await asyncio.to_thread(session.adapter.is_alive):
                        break
                    continue
                async with session.lock:
                    if session.log_handle is not None:
                        session.log_handle.write(text)
                        session.log_handle.flush()
                    previous_title = session.renderer.title
                    session.renderer.feed(text)
                    title_changed = session.renderer.title != previous_title
                    self._publish_output(session, text)
                    state_changed = False
                    if session.state != "starting":
                        state_changed = session.state != "working"
                        session.state = "working"
                        self._schedule_settle(session, notify=False)
                    if state_changed or title_changed:
                        self._publish_state(session)
                    session.output_event.set()
        except asyncio.CancelledError:
            raise
        except (EOFError, OSError):
            pass
        except BaseException as caught:
            error = caught
        finally:
            await self._mark_finished(session, error)

    def _schedule_settle(self, session: TerminalSession, *, notify: bool) -> None:
        """Restart the generic quiet timer after PTY input or output activity."""
        attention = session.attention
        pending_agent_delivery = (
            attention is not None
            and attention.kind == "output_settled"
            and not attention.delivered
            and session.notification_task is not None
            and not session.notification_task.done()
        )
        if pending_agent_delivery:
            self._cancel_delivery(session)
            notify = True
        session.activity_generation += 1
        generation = session.activity_generation
        session.notify_on_settle = session.notify_on_settle or notify
        previous = session.settle_task
        if previous is not None and not previous.done():
            previous.cancel()
        session.settle_task = asyncio.create_task(
            self._settle_after_quiet(session, generation),
            name=f"terminal:{session.terminal_id}:settle:{generation}",
        )
        session.settle_task.add_done_callback(
            lambda task: _log_background_task_result(
                task,
                f"Terminal quiet detection failed for terminal={session.terminal_id} "
                f"generation={generation}",
            )
        )

    async def _settle_after_quiet(self, session: TerminalSession, generation: int) -> None:
        try:
            await asyncio.sleep(self._activity_quiet_seconds)
            async with session.lock:
                if (
                    generation != session.activity_generation
                    or session.state in {"starting", "exited", "error"}
                    or session.finished_at is not None
                ):
                    return
                deliver = session.notify_on_settle
                session.notify_on_settle = False
                session.state = "ready"
                self._set_attention(
                    session,
                    kind="output_settled",
                    summary=(
                        "Terminal output has been quiet after recent activity. Inspect the "
                        "current screen; this does not imply that the program finished or "
                        "requires input."
                    ),
                    details={"screen_revision": session.renderer.revision},
                    deliver=deliver,
                )
        except asyncio.CancelledError:
            return

    async def _mark_finished(self, session: TerminalSession, error: BaseException | None) -> None:
        initial_task = session.initial_input_task
        if (
            initial_task is not None
            and initial_task is not asyncio.current_task()
            and not initial_task.done()
        ):
            initial_task.cancel()
        settle_task = session.settle_task
        if (
            settle_task is not None
            and settle_task is not asyncio.current_task()
            and not settle_task.done()
        ):
            settle_task.cancel()
        async with session.lock:
            if session.state in {"exited", "error"}:
                return
            session.finished_at = session.finished_at or _utc_now()
            session.exit_code = await asyncio.to_thread(session.adapter.exit_code)
            if error is None:
                session.state = "exited"
                if not session.suppress_exit_attention:
                    self._set_attention(
                        session,
                        kind="exited",
                        summary=f"Terminal process exited with code {session.exit_code}.",
                        details={"exit_code": session.exit_code},
                    )
                else:
                    self._publish_state(session)
            else:
                session.state = "error"
                if not session.suppress_exit_attention:
                    self._set_attention(
                        session,
                        kind="error",
                        summary="Terminal transport or rendering failed.",
                        details={"error": str(error)},
                    )
                else:
                    self._publish_state(session)
            session.attention_event.set()
            session.output_event.set()
            self._finish_files(session)

    def _set_attention(
        self,
        session: TerminalSession,
        *,
        kind: AttentionKind,
        summary: str,
        details: dict[str, Any],
        deliver: bool = True,
    ) -> None:
        self._cancel_delivery(session)
        session.attention_revision += 1
        revision = session.attention_revision
        attention = TerminalAttention(
            revision=revision,
            kind=kind,
            notice_id=f"terminal:{session.terminal_id}:attention:{revision}",
            summary=summary,
            details=details,
            created_at=_utc_now(),
        )
        session.attention = attention
        session.attention_event.set()
        if deliver:
            self._schedule_attention_delivery(session, attention)
        self._publish_state(session)

    def _schedule_attention_delivery(
        self, session: TerminalSession, attention: TerminalAttention
    ) -> None:
        if self._trigger_service is None or session.owner is None:
            return
        revision = attention.revision
        session.notification_task = asyncio.create_task(
            self._deliver_attention(session, attention),
            name=f"terminal:{session.terminal_id}:attention:{revision}",
        )
        session.notification_task.add_done_callback(
            lambda task: _log_background_task_result(
                task,
                f"Terminal attention delivery failed for terminal={session.terminal_id} "
                f"revision={revision}",
            )
        )

    async def _deliver_attention(
        self, session: TerminalSession, attention: TerminalAttention
    ) -> None:
        trigger_service = self._trigger_service
        owner = session.owner
        if trigger_service is None or owner is None:
            return
        origin_run_id = session.activity_origin_run_id or session.origin_run_id
        if origin_run_id is None:
            return
        delivery = trigger_service.submit_completion(
            owner.agent_id,
            owner.session_id,
            notice_id=attention.notice_id,
            origin_run_id=origin_run_id,
            body=_attention_body(session, attention),
            project_id=owner.project_id,
        )
        await delivery
        attention.delivered = True

    async def _terminate_session(
        self, session: TerminalSession, *, suppress_attention: bool
    ) -> None:
        initial_task = session.initial_input_task
        if (
            initial_task is not None
            and initial_task is not asyncio.current_task()
            and not initial_task.done()
        ):
            initial_task.cancel()
        if suppress_attention:
            session.suppress_exit_attention = True
            self._cancel_delivery(session)
        settle_task = session.settle_task
        if settle_task is not None and not settle_task.done():
            settle_task.cancel()
        if session.state not in {"exited", "error"}:
            await asyncio.to_thread(terminate_process_tree, session.adapter)
        reader = session.reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=5)
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        if session.state not in {"exited", "error"}:
            await self._mark_finished(session, None)

    def _cancel_delivery(self, session: TerminalSession) -> None:
        task = session.notification_task
        attention = session.attention
        owner = session.owner
        if task is None or task.done() or attention is None or owner is None:
            return
        if self._trigger_service is not None:
            self._trigger_service.cancel_completion(
                owner.agent_id,
                owner.session_id,
                notice_id=attention.notice_id,
                project_id=owner.project_id,
            )
        task.cancel()

    def _snapshot_data(
        self, session: TerminalSession, scrollback: dict[str, Any]
    ) -> dict[str, Any]:
        attention = session.attention
        return {
            "terminal_id": session.terminal_id,
            "state": session.state,
            "command": session.command,
            "title": session.renderer.title,
            "arguments": list(session.arguments),
            "workdir": str(session.cwd),
            "pid": session.adapter.pid,
            "exit_code": session.exit_code,
            "started_at": session.started_at.isoformat(),
            "finished_at": session.finished_at.isoformat() if session.finished_at else None,
            "columns": session.renderer.columns,
            "rows": session.renderer.rows,
            "screen_revision": session.renderer.revision,
            "attention_revision": session.attention_revision,
            "screen": session.renderer.screen_text(),
            "scrollback": scrollback,
            "attention": _attention_data(attention),
            "log_file": str(session.log_path) if session.log_path is not None else None,
        }

    def _get_for_operator(self, terminal_id: str) -> TerminalSession:
        session = self._sessions.get(terminal_id)
        if session is None:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return session

    def _operator_summary(self, session: TerminalSession) -> dict[str, Any]:
        attention = session.attention
        owner = session.owner
        return {
            "terminal_id": session.terminal_id,
            "state": session.state,
            "command": Path(session.command).name or session.command,
            "title": session.renderer.title,
            "workdir": str(session.cwd),
            "pid": session.adapter.pid,
            "started_at": session.started_at.isoformat(),
            "finished_at": session.finished_at.isoformat() if session.finished_at else None,
            "columns": session.renderer.columns,
            "rows": session.renderer.rows,
            "screen_revision": session.renderer.revision,
            "owner": (
                {
                    "project_id": owner.project_id,
                    "agent_id": owner.agent_id,
                    "session_id": owner.session_id,
                }
                if owner is not None
                else None
            ),
            "attention": (
                {
                    "revision": attention.revision,
                    "kind": attention.kind,
                    "summary": attention.summary,
                    "created_at": attention.created_at.isoformat(),
                }
                if attention is not None
                else None
            ),
        }

    def _publish_output(self, session: TerminalSession, text: str) -> None:
        session.stream_sequence += 1
        session.stream.publish(
            {
                "type": "terminal_output",
                "sequence": session.stream_sequence,
                "data": text,
            }
        )

    def _publish_state(self, session: TerminalSession) -> None:
        session.stream_sequence += 1
        session.stream.publish(
            {
                "type": "terminal_state",
                "sequence": session.stream_sequence,
                "terminal": self._operator_summary(session),
            }
        )
        self._notify_changed(session.terminal_id)

    def _notify_changed(self, terminal_id: str) -> None:
        for callback in list(self._changed_callbacks):
            try:
                callback(terminal_id)
            except Exception:
                _LOGGER.exception("Terminal changed callback failed for terminal=%s", terminal_id)

    def _enforce_capacity(self, owner: TerminalOwner | None) -> None:
        live = [
            session
            for session in self._sessions.values()
            if session.state not in {"exited", "error"}
        ]
        if len(live) >= TERMINAL_MAX_LIVE_GLOBAL:
            raise TerminalCapacityError(
                f"Live Terminal Session limit reached ({TERMINAL_MAX_LIVE_GLOBAL})"
            )
        owned = sum(1 for session in live if owner is not None and session.owner == owner)
        if owner is not None and owned >= TERMINAL_MAX_LIVE_PER_SESSION:
            raise TerminalCapacityError(
                "Live Terminal Session limit reached for this vBot Session "
                f"({TERMINAL_MAX_LIVE_PER_SESSION})"
            )

    def _open_raw_log(
        self,
    ) -> tuple[Path | None, TextIO | None, TemporaryFileLease | None]:
        if self._temporary_files is None:
            return None, None, None
        lease = self._temporary_files.create(TERMINAL_TEMPORARY_CATEGORY, ".log")
        return lease.path, lease.path.open("a", encoding="utf-8", newline=""), lease

    @staticmethod
    def _finish_files(session: TerminalSession) -> None:
        if session.log_handle is not None:
            with contextlib.suppress(OSError):
                session.log_handle.close()
            session.log_handle = None
        if session.log_lease is not None:
            session.log_lease.finish()
            session.log_lease = None

    @staticmethod
    def _require_live(session: TerminalSession) -> None:
        if session.finished_at is not None or session.state in {"exited", "error"}:
            raise TerminalClosedError("Terminal Session is no longer running")

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return


def _input_chunks(
    *, data: str | None, text: str | None, key: str | None, enter: bool
) -> tuple[str, ...]:
    if data is not None:
        if text is not None or key is not None or enter:
            raise ValueError("data cannot be combined with text, key, or enter")
        if not data:
            raise ValueError("data must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"data must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        return (data,)
    if key is not None and key not in TERMINAL_INPUT_KEY_SEQUENCES:
        raise ValueError(f"Unsupported terminal key: {key}")
    chunks: list[str] = []
    if text is not None:
        if not text:
            raise ValueError("text must be non-empty when provided")
        if len(text) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"text must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        chunks.append(text)
    if key is not None:
        chunks.append(TERMINAL_INPUT_KEY_SEQUENCES[key])
    if enter:
        chunks.append("\r")
    if not chunks:
        raise ValueError("input must send text, a key, or Enter")
    return tuple(chunks)


def _attention_body(session: TerminalSession, attention: TerminalAttention) -> str:
    heading = {
        "output_settled": "Terminal output settled",
        "exited": "Terminal process exited",
        "error": "Terminal failure",
    }[attention.kind]
    sections = [
        f"### Terminal Session — {heading}",
        f"Terminal id: {session.terminal_id}",
        f"State: {session.state}",
        f"Attention revision: {attention.revision}",
        attention.summary,
    ]
    if attention.kind == "output_settled":
        sections.extend(
            (
                "Use terminal_beta status to inspect the current screen. Decide from that "
                "screen whether the program is still working, is waiting for input, has "
                "returned to a prompt, or needs no action. Reuse this Terminal Session; do "
                "not start a duplicate process.",
            )
        )
    elif attention.details:
        sections.append(json.dumps(attention.details, ensure_ascii=False, indent=2))
    body = "\n".join(sections)
    if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS:
        return body
    return body[:TERMINAL_NOTICE_MESSAGE_CAP_CHARS] + "\n[attention details truncated]"


def _attention_data(attention: TerminalAttention | None) -> dict[str, Any] | None:
    if attention is None:
        return None
    return {
        "revision": attention.revision,
        "kind": attention.kind,
        "summary": attention.summary,
        "details": attention.details,
        "created_at": attention.created_at.isoformat(),
        "delivered": attention.delivered,
    }


def _validate_owner(owner: TerminalOwner) -> None:
    if not owner.agent_id or not owner.session_id:
        raise ValueError("Terminal owner Agent and Session ids are required")


def _validate_dimensions(columns: int, rows: int) -> None:
    if not TERMINAL_MIN_COLUMNS <= columns <= TERMINAL_MAX_COLUMNS:
        raise ValueError(
            f"columns must be between {TERMINAL_MIN_COLUMNS} and {TERMINAL_MAX_COLUMNS}"
        )
    if not TERMINAL_MIN_ROWS <= rows <= TERMINAL_MAX_ROWS:
        raise ValueError(f"rows must be between {TERMINAL_MIN_ROWS} and {TERMINAL_MAX_ROWS}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _log_background_task_result(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "%s: %s",
        message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


__all__ = [
    "AttentionKind",
    "TERMINAL_DEFAULT_COLUMNS",
    "TERMINAL_DEFAULT_ROWS",
    "TERMINAL_FINISHED_TTL",
    "TERMINAL_INPUT_MAX_CHARS",
    "TERMINAL_INPUT_KEY_SEQUENCES",
    "TERMINAL_MAX_COLUMNS",
    "TERMINAL_MAX_ROWS",
    "TERMINAL_MIN_COLUMNS",
    "TERMINAL_MIN_ROWS",
    "TERMINAL_STATUS_DEFAULT_LINES",
    "TERMINAL_STATUS_MAX_LINES",
    "TERMINAL_TEMPORARY_CATEGORY",
    "TerminalAdapter",
    "TerminalCapacityError",
    "TerminalClosedError",
    "TerminalCursorError",
    "TerminalLaunchError",
    "TerminalManager",
    "TerminalManagerError",
    "TerminalNotFoundError",
    "TerminalOwner",
    "TerminalSession",
    "TerminalStaleScreenError",
    "TerminalState",
]
