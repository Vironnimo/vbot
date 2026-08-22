"""Session-scoped interactive PTY/ConPTY lifecycle and activity management."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO

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
from core.utils.atomic import atomic_write_text
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.paths import model_path

_LOGGER = get_logger("tools.terminal_manager")

TERMINAL_DEFAULT_COLUMNS = 120
TERMINAL_DEFAULT_ROWS = 32
TERMINAL_MIN_COLUMNS = 40
TERMINAL_MAX_COLUMNS = 240
TERMINAL_MIN_ROWS = 10
TERMINAL_MAX_ROWS = 80
TERMINAL_SCROLLBACK_LINES = 2_000
TERMINAL_STATUS_DEFAULT_LINES = 30
TERMINAL_STATUS_MAX_LINES = 100
TERMINAL_MAX_LIVE_PER_SESSION = 4
TERMINAL_MAX_LIVE_GLOBAL = 32
TERMINAL_SWEEP_INTERVAL_SECONDS = 60.0
TERMINAL_FINISHED_TTL = timedelta(minutes=30)
TERMINAL_NOTICE_MESSAGE_CAP_CHARS = 16_000
# How many newest screen rows the automatic attention notice embeds, so the
# woken Agent can usually act without a follow-up status call.
TERMINAL_DELIVERY_TAIL_LINES = 20
TERMINAL_TEMPORARY_CATEGORY = "terminals"
TERMINAL_INITIAL_INPUT_QUIET_SECONDS = 0.5
TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS = 15.0
TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS = 10.0
TERMINAL_ACTIVITY_QUIET_SECONDS = 2.0
# After a real dimension change the foreground program repaints its screen.
# That repaint is viewer noise rather than work, so output that settles inside
# this grace window after an explicit resize must not wake the agent session.
TERMINAL_RESIZE_GRACE_SECONDS = 4.0
TERMINAL_INPUT_KEY_DELAY_SECONDS = 0.1
TERMINAL_STREAM_RETENTION_EVENTS = 4_096
TERMINAL_STREAM_SUBSCRIBER_QUEUE_EVENTS = 512
TERMINAL_INPUT_MAX_CHARS = 65_536
TERMINAL_BRACKETED_PASTE_START = "\x1b[200~"
TERMINAL_BRACKETED_PASTE_END = "\x1b[201~"
TERMINAL_LAUNCH_HISTORY_VERSION = 1
TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES = 50
TERMINAL_GROUPS_FILE_NAME = "groups.json"
TERMINAL_GROUPS_VERSION = 1
TERMINAL_GROUP_NAME_MAX_CHARS = 80
TERMINAL_FINISHED_GROUP_ID = "finished"
TERMINAL_MANUAL_GROUP_ID = "auto:manual"
TERMINAL_AGENT_GROUP_ID_PREFIX = "auto:agent:"
GroupKind = Literal["user", "agent", "automatic", "finished"]
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


class TerminalAlreadyAttachedError(TerminalManagerError):
    """Raised when another vBot Session already holds the one attachment."""


class TerminalNotAttachedError(TerminalManagerError):
    """Raised when detach does not target the current attachment."""


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
    """Exact vBot Session address used by Terminal lifecycle and attachment scopes."""

    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class TerminalLaunchHistoryEntry:
    """One durable, most-recently-used manual Terminal launch."""

    id: str
    command: str | None
    arguments: tuple[str, ...]
    workdir: str | None
    used_at: datetime


@dataclass(slots=True)
class TerminalGroup:
    """One operator-visible collection of Terminal Sessions.

    ``kind`` decides durability and lifecycle:
    - ``user`` groups are persisted and stay when empty.
    - ``agent`` groups are created by the terminal Tool and live in memory.
    - ``automatic`` groups are synthesized per Agent and for manual terminals,
      never durable, and removed when empty.
    - ``finished`` is the single retention group for exited/error terminals.
    """

    group_id: str
    name: str
    kind: GroupKind
    order: list[str]
    created_at: datetime
    source: str | None = None


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
    # Immutable process provenance. None means the local operator started it.
    owner: TerminalOwner | None
    # Agent-started terminals retain their lifecycle scope independently of attachment.
    lifecycle_owner: TerminalOwner | None
    # The one vBot Session currently authorized for Tool access and activity delivery.
    attachment: TerminalOwner | None
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
    launch_command: str | None = None
    launch_arguments: tuple[str, ...] = ()
    name: str | None = None
    # Explicit user/agent group chosen at spawn; None means the Terminal
    # belongs to its automatic group (per-Agent or shared manual).
    group_id: str | None = None
    exit_code: int | None = None
    finished_at: datetime | None = None
    attention_revision: int = 0
    acknowledged_attention_revision: int = 0
    attention: TerminalAttention | None = None
    activity_generation: int = 0
    notify_on_settle: bool = False
    settled_delivery_enabled: bool = False
    # Monotonic deadline until which settle notifications are suppressed because
    # the output is expected to be a repaint after an explicit resize.
    resize_grace_until: float = 0.0
    snapshot_on_settle: bool = False
    suppress_exit_attention: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    output_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    attention_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    initial_input_task: asyncio.Task[None] | None = field(default=None, repr=False)
    operator_command_task: asyncio.Task[None] | None = field(default=None, repr=False)
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
        launch_history_path: Path | None = None,
        groups_path: Path | None = None,
        data_dir: Path | None = None,
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
        self._launch_history_path = launch_history_path
        self._groups_path = groups_path
        self._data_dir = data_dir
        self._adapter_factory = adapter_factory or spawn_terminal_adapter
        self._scrollback_lines = scrollback_lines
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._activity_quiet_seconds = activity_quiet_seconds
        self._sessions: dict[str, TerminalSession] = {}
        self._changed_callbacks: list[TerminalChangedCallback] = []
        self._launch_history = self._load_launch_history()
        self._groups = self._load_groups()
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
            for task in (
                session.reader_task,
                session.initial_input_task,
                session.operator_command_task,
                session.settle_task,
            ):
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
                session.operator_command_task,
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
        columns: int = TERMINAL_DEFAULT_COLUMNS,
        rows: int = TERMINAL_DEFAULT_ROWS,
        origin_run_id: str,
        initial_text: str | None = None,
        name: str | None = None,
        group_id: str | None = None,
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
            name=name,
            group_id=group_id,
        )

    async def spawn_for_operator(
        self,
        *,
        command: str | None,
        arguments: Sequence[str],
        cwd: Path | None,
        launch_workdir: str | None = None,
        name: str | None = None,
        group_id: str | None = None,
        columns: int = TERMINAL_DEFAULT_COLUMNS,
        rows: int = TERMINAL_DEFAULT_ROWS,
    ) -> dict[str, Any]:
        """Start one manual Terminal Session that behaves like a normal terminal.

        A requested command runs inside the interactive shell instead of as the
        bare PTY child, so the Session keeps a live prompt after the program
        ends, is interrupted, or is stopped. The command is entered through the
        existing initial-input path, which means the exact launch command stays
        available as metadata and the Agent-owned spawn path (exact argv) is
        untouched.
        """
        if command is None:
            argv = default_terminal_argv()
            argv.extend(arguments)
            launch_command = None
            launch_arguments: tuple[str, ...] = ()
        else:
            argv = default_terminal_argv()
            launch_command = command
            launch_arguments = tuple(arguments)
        session = await self._spawn(
            None,
            argv,
            cwd=cwd or Path.home(),
            env=None,
            columns=columns,
            rows=rows,
            origin_run_id=None,
            name=name,
            launch_command=launch_command,
            launch_arguments=launch_arguments,
            group_id=group_id,
        )
        if launch_command is not None:
            session.operator_command_task = asyncio.create_task(
                self._send_operator_command(session),
                name=f"terminal:{session.terminal_id}:operator-command",
            )
            session.operator_command_task.add_done_callback(
                lambda task: _log_background_task_result(
                    task, f"Terminal operator command failed for terminal={session.terminal_id}"
                )
            )
        remembered_workdir = launch_workdir
        if remembered_workdir is None and cwd is not None:
            remembered_workdir = str(cwd)
        self._remember_operator_launch(
            command=launch_command,
            arguments=launch_arguments,
            workdir=remembered_workdir,
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
        name: str | None = None,
        launch_command: str | None = None,
        launch_arguments: tuple[str, ...] = (),
        group_id: str | None = None,
    ) -> TerminalSession:
        """Start one unmodified program behind PTY/ConPTY."""
        if not argv or not argv[0]:
            raise ValueError("Terminal command must not be empty")
        if any(not isinstance(token, str) for token in argv):
            raise ValueError("Terminal command and arguments must be strings")
        _validate_dimensions(columns, rows)
        if not cwd.is_dir():
            raise ValueError(f"Terminal workdir is not a directory: {model_path(cwd)}")
        if group_id is not None:
            self._require_group(group_id)
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
            lifecycle_owner=owner,
            attachment=owner,
            adapter=adapter,
            renderer=TerminalRenderer(columns, rows, scrollback_lines=self._scrollback_lines),
            command=argv[0],
            arguments=tuple(argv[1:]),
            launch_command=launch_command,
            launch_arguments=launch_arguments,
            name=name,
            group_id=group_id,
            cwd=cwd,
            state="starting" if initial_text is not None else "ready",
            started_at=_utc_now(),
            origin_run_id=origin_run_id,
            activity_origin_run_id=None,
            log_path=log_path,
            log_handle=log_handle,
            log_lease=log_lease,
        )
        if group_id is not None:
            self._append_group_terminal(group_id, terminal_id)
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

    def list_sessions(self) -> list[TerminalSession]:
        """Return all retained Terminal Sessions discoverable for attachment."""
        return sorted(
            self._sessions.values(),
            key=lambda session: session.started_at,
        )

    def get_session(self, terminal_id: str, owner: TerminalOwner) -> TerminalSession:
        """Return a Terminal Session attached to the exact vBot Session."""
        session = self._sessions.get(terminal_id)
        if session is None or session.attachment != owner:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return session

    def attach(
        self,
        terminal_id: str,
        attachment: TerminalOwner,
        *,
        origin_run_id: str,
    ) -> tuple[TerminalSession, bool]:
        """Attach one live Terminal Session without changing its process or lifecycle."""
        _validate_owner(attachment)
        session = self._get_for_operator(terminal_id)
        self._require_live(session)
        if session.attachment is not None and session.attachment != attachment:
            raise TerminalAlreadyAttachedError(
                "Terminal Session is already attached to another vBot Session."
            )
        changed = session.attachment is None
        session.attachment = attachment
        session.activity_origin_run_id = origin_run_id
        session.acknowledged_attention_revision = session.attention_revision
        session.settled_delivery_enabled = True
        if session.state == "working":
            self._schedule_settle(session, notify=True)
        if changed:
            self._publish_state(session)
            _LOGGER.info(
                "Attached Terminal Session terminal=%s agent=%s session=%s project=%s",
                terminal_id,
                attachment.agent_id,
                attachment.session_id,
                attachment.project_id,
            )
        return session, changed

    def detach(self, terminal_id: str, attachment: TerminalOwner) -> TerminalSession:
        """Remove only one exact vBot Session attachment."""
        session = self._get_for_operator(terminal_id)
        if session.attachment != attachment:
            raise TerminalNotAttachedError("Terminal Session is not attached to this vBot Session.")
        self._detach_session(session)
        self._publish_state(session)
        _LOGGER.info(
            "Detached Terminal Session terminal=%s agent=%s session=%s project=%s",
            terminal_id,
            attachment.agent_id,
            attachment.session_id,
            attachment.project_id,
        )
        return session

    def list_for_operator(self) -> list[dict[str, Any]]:
        """Return retained Terminal Sessions grouped and ordered for display."""
        rank: dict[str, int] = {
            group["group_id"]: index for index, group in enumerate(self.list_groups_for_operator())
        }

        def sort_key(session: TerminalSession) -> tuple[int, int, int, float]:
            group_id = self._session_group(session).group_id
            group = self._groups.get(group_id)
            position = len(group.order) + 1 if group is not None else -1
            if group is not None:
                try:
                    position = group.order.index(session.terminal_id)
                except ValueError:
                    position = len(group.order) + 1
            stamp = session.finished_at if session.finished_at else session.started_at
            return (rank.get(group_id, 10**9), position, 0, -stamp.timestamp())

        sessions = sorted(self._sessions.values(), key=sort_key)
        return [self._operator_summary(session) for session in sessions]

    def list_operator_launch_history(self) -> list[dict[str, Any]]:
        """Return newest-first manual launch configurations for operator reuse."""
        return [
            {
                "id": entry.id,
                "command": entry.command,
                "args": list(entry.arguments),
                "workdir": entry.workdir,
                "used_at": entry.used_at.isoformat(),
            }
            for entry in self._launch_history
        ]

    def list_groups_for_operator(self) -> list[dict[str, Any]]:
        """Return operator-visible groups: user/agent groups, then the shared
        manual automatic group, then one automatic group per active Agent."""
        groups = list(self._groups.values())
        automatic = [
            self._automatic_group(TERMINAL_MANUAL_GROUP_ID)
            if self._automatic_group_terminals(TERMINAL_MANUAL_GROUP_ID)
            else None,
            *(
                self._automatic_group(agent_group_id(owner.agent_id))
                for owner in self._distinct_agent_owners()
                if self._automatic_group_terminals(agent_group_id(owner.agent_id))
            ),
        ]
        for group in automatic:
            if group is not None and not any(
                existing.group_id == group.group_id for existing in groups
            ):
                groups.append(group)
        if self._finished_group_terminals():
            groups.append(self._finished_group())
        return [self._group_summary(group) for group in groups]

    def create_group_for_operator(self, name: str) -> dict[str, Any]:
        """Create one durable user group with a unique name."""
        name = _validate_group_name(name)
        if self._group_name_taken(name):
            raise TerminalManagerError(f"A Terminal group named '{name}' already exists")
        group = TerminalGroup(
            group_id=uuid.uuid4().hex,
            name=name,
            kind="user",
            order=[],
            created_at=_utc_now(),
        )
        self._groups[group.group_id] = group
        self._persist_groups()
        self._notify_changed("")
        _LOGGER.info("Created Terminal group group=%s name=%s", group.group_id, group.name)
        return self._group_summary(group)

    def rename_group_for_operator(self, group_id: str, name: str) -> dict[str, Any]:
        """Rename one user or agent group; automatic groups are fixed."""
        group = self._require_group(group_id)
        if group.kind == "automatic" or group.kind == "finished":
            raise TerminalManagerError("This Terminal group cannot be renamed")
        name = _validate_group_name(name)
        if self._group_name_taken(name, exclude=group_id):
            raise TerminalManagerError(f"A Terminal group named '{name}' already exists")
        previous = group.name
        group.name = name
        if group.kind == "user":
            self._persist_groups()
        self._notify_changed("")
        _LOGGER.info("Renamed Terminal group group=%s from=%s to=%s", group_id, previous, name)
        return self._group_summary(group)

    async def delete_group_for_operator(self, group_id: str) -> dict[str, Any]:
        """Remove one user or agent group and kill every live terminal in it.

        Killed terminals stay in the retained catalog and appear in the
        finished group, exactly like an explicit kill.
        """
        group = self._require_group(group_id)
        if group.kind == "automatic" or group.kind == "finished":
            raise TerminalManagerError("This Terminal group cannot be deleted")
        terminals = [
            session
            for session in self._sessions.values()
            if self._session_group(session).group_id == group_id
        ]
        for session in terminals:
            if session.state not in {"exited", "error"}:
                await self._terminate_session(session, suppress_attention=True)
        del self._groups[group_id]
        if group.kind == "user":
            self._persist_groups()
        self._notify_changed("")
        _LOGGER.info(
            "Deleted Terminal group group=%s name=%s terminals=%d",
            group_id,
            group.name,
            len(terminals),
        )
        return {"group_id": group_id, "name": group.name, "terminals_killed": len(terminals)}

    def set_group_order_for_operator(self, group_id: str, order: Sequence[str]) -> dict[str, Any]:
        """Persist one user-set Terminal order; missing ids are appended."""
        group = self._require_group(group_id)
        if not isinstance(order, list) or any(
            not isinstance(item, str) or not item for item in order
        ):
            raise ValueError("order must be a list of terminal ids")
        seen: set[str] = set()
        for terminal_id in order:
            if terminal_id in seen:
                raise ValueError("order must not contain duplicate terminal ids")
            seen.add(terminal_id)
        members = self._group_members(group.group_id)
        unknown = seen - {session.terminal_id for session in members}
        if unknown:
            raise TerminalManagerError("order contains terminals that do not belong to this group")
        ordered = list(order)
        ordered.extend(
            session.terminal_id for session in members if session.terminal_id not in seen
        )
        group.order = ordered
        if group.kind == "user":
            self._persist_groups()
        self._notify_changed("")
        return {"group_id": group_id, "order": list(ordered)}

    def resolve_or_create_agent_group(self, name: str) -> TerminalGroup:
        """Return the group with this name or create a non-durable Agent group.

        The lookup spans user and agent groups, so an Agent reuses the group
        the operator already set up (or another Agent created).
        """
        name = _validate_group_name(name)
        existing = self._group_by_name(name)
        if existing is not None:
            return existing
        group = TerminalGroup(
            group_id=uuid.uuid4().hex,
            name=name,
            kind="agent",
            order=[],
            created_at=_utc_now(),
            source=None,
        )
        self._groups[group.group_id] = group
        self._notify_changed("")
        _LOGGER.info("Created Agent Terminal group group=%s name=%s", group.group_id, name)
        return group

    def _load_launch_history(self) -> list[TerminalLaunchHistoryEntry]:
        path = self._launch_history_path
        if path is None or not path.exists():
            return []
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            return _parse_launch_history(document)
        except (OSError, UnicodeError, ValueError) as error:
            _LOGGER.warning("Could not load Terminal launch history from '%s': %s", path, error)
            return []

    def _remember_operator_launch(
        self,
        *,
        command: str | None,
        arguments: Sequence[str],
        workdir: str | None,
    ) -> None:
        entry = TerminalLaunchHistoryEntry(
            id=_launch_history_id(command, arguments, workdir),
            command=command,
            arguments=tuple(arguments),
            workdir=workdir,
            used_at=_utc_now(),
        )
        self._launch_history = [
            entry,
            *(item for item in self._launch_history if item.id != entry.id),
        ][:TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES]
        self._persist_launch_history()

    def _persist_launch_history(self) -> None:
        path = self._launch_history_path
        if path is None:
            return
        document = {
            "version": TERMINAL_LAUNCH_HISTORY_VERSION,
            "entries": self.list_operator_launch_history(),
        }
        try:
            atomic_write_text(
                path,
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                data_dir=self._data_dir,
            )
        except OSError as error:
            _LOGGER.warning("Could not persist Terminal launch history to '%s': %s", path, error)

    def _load_groups(self) -> dict[str, TerminalGroup]:
        path = self._groups_path
        if path is None or not path.exists():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            groups = _parse_groups(document)
        except (OSError, UnicodeError, ValueError) as error:
            _LOGGER.warning("Could not load Terminal groups from '%s': %s", path, error)
            return {}
        return {group.group_id: group for group in groups}

    def _persist_groups(self) -> None:
        path = self._groups_path
        if path is None:
            return
        document = {
            "version": TERMINAL_GROUPS_VERSION,
            "groups": [
                {
                    "id": group.group_id,
                    "name": group.name,
                    "order": list(group.order),
                    "created_at": group.created_at.isoformat(),
                }
                for group in self._groups.values()
                if group.kind == "user"
            ],
        }
        try:
            atomic_write_text(
                path,
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                data_dir=self._data_dir,
            )
        except OSError as error:
            _LOGGER.warning("Could not persist Terminal groups to '%s': %s", path, error)

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
        command_task = session.operator_command_task
        if command_task is not None and not command_task.done():
            # The launch command is typed into the shell while it is still
            # booting; user input must queue behind it instead of racing it
            # into the same line (which would corrupt the command). The task
            # always terminates: its shell-readiness wait is bounded.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(command_task),
                    timeout=TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS + 1.0,
                )
        async with session.lock:
            self._require_live(session)
            state_changed = session.state != "working"
            session.state = "working"
            await asyncio.to_thread(session.adapter.write, data)
            self._schedule_settle(session, notify=session.attachment is not None)
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
        start_line: int | None = None,
        include_name: bool = True,
    ) -> dict[str, Any]:
        """Return one bounded rendered status page.

        ``start_line`` addresses the whole buffer by absolute zero-based line
        (Hermes ``read_terminal`` contract); omit it for the newest page.
        """
        if not 1 <= lines <= TERMINAL_STATUS_MAX_LINES:
            raise ValueError(f"lines must be between 1 and {TERMINAL_STATUS_MAX_LINES}")
        if start_line is not None and start_line < 0:
            raise ValueError("start_line must be a non-negative integer")
        session = self.get_session(terminal_id, owner)
        async with session.lock:
            try:
                if start_line is not None:
                    scrollback = session.renderer.page_from(start_line, lines)
                else:
                    scrollback = session.renderer.page(before=None, limit=lines)
            except ValueError as error:
                raise TerminalCursorError(str(error)) from error
            data = self._snapshot_data(session, scrollback)
            if include_name:
                data["name"] = session.name
            return data

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
        return await self.snapshot(terminal_id, owner, include_name=False), timed_out

    async def send_input(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        text: str | None,
        key: str | None,
        expected_screen_revision: int | None,
        origin_run_id: str,
        data: str | None = None,
    ) -> dict[str, Any]:
        """Write exact data or named terminal input and track generic PTY activity."""
        session = self.get_session(terminal_id, owner)
        initial_task = session.initial_input_task
        async with session.lock:
            self._require_live(session)
            if (
                expected_screen_revision is not None
                and expected_screen_revision != session.renderer.revision
            ):
                raise TerminalStaleScreenError(
                    "Terminal screen changed; inspect status before sending this input"
                )
            bracketed_paste = (
                text is not None
                and ("\n" in text or "\r" in text)
                and session.renderer.bracketed_paste_enabled
            )
            chunks = _input_chunks(
                data=data,
                text=text,
                key=key,
                bracketed_paste=bracketed_paste,
            )
            if not chunks:
                return {
                    "terminal_id": terminal_id,
                    "state": session.state,
                    "characters_sent": 0,
                    "key": key,
                    "bracketed_paste": False,
                    "superseded_attention_revision": None,
                    "screen_revision": session.renderer.revision,
                }
            if (
                initial_task is not None
                and initial_task is not asyncio.current_task()
                and not initial_task.done()
            ):
                initial_task.cancel()
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
                "bracketed_paste": bracketed_paste,
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
            attachment = session.attachment
            if attachment is None:
                return
            await self.send_input(
                session.terminal_id,
                attachment,
                data=None,
                text=text,
                key="enter",
                expected_screen_revision=None,
                origin_run_id=origin_run_id,
            )
        except asyncio.CancelledError:
            return

    async def _send_operator_command(self, session: TerminalSession) -> None:
        """Enter one operator-requested command into an interactive shell.

        The shell owns the Session and stays alive after the command ends, so
        the command is written through the exact `data` channel exactly once,
        without bracketed-paste or Enter state.
        """
        command = _shell_command(session.launch_command, session.launch_arguments)
        if command is None:
            return
        if not await _await_shell_ready(session):
            return
        try:
            await asyncio.to_thread(session.adapter.write, command)
            await asyncio.to_thread(session.adapter.write, "\r")
        except (EOFError, OSError):
            return
        except asyncio.CancelledError:
            return
        async with session.lock:
            if session.state not in {"exited", "error"} and session.state != "working":
                session.state = "working"
                self._publish_state(session)
            session.output_event.set()

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
            if columns == session.renderer.columns and rows == session.renderer.rows:
                # A resize to the current dimensions is a no-op: forwarding it
                # would make the foreground program repaint and wake the agent.
                return {
                    "terminal_id": session.terminal_id,
                    "state": session.state,
                    "columns": columns,
                    "rows": rows,
                    "screen_revision": session.renderer.revision,
                }
            await asyncio.to_thread(session.adapter.resize, rows, columns)
            session.renderer.resize(columns, rows)
            session.resize_grace_until = time.monotonic() + TERMINAL_RESIZE_GRACE_SECONDS
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
        return await self.snapshot(terminal_id, owner, include_name=False)

    async def close_scope(self, owner: TerminalOwner) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Session."""
        sessions = [
            session for session in self._sessions.values() if session.lifecycle_owner == owner
        ]
        for session in self._sessions.values():
            if session.lifecycle_owner != owner and session.attachment == owner:
                self._detach_session(session)
                self._publish_state(session)
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Agent scope."""
        sessions = [
            session
            for session in self._sessions.values()
            if session.lifecycle_owner is not None
            and session.lifecycle_owner.agent_id == agent_id
            and session.lifecycle_owner.project_id == project_id
        ]
        terminating = {session.terminal_id for session in sessions}
        for session in self._sessions.values():
            attachment = session.attachment
            if (
                session.terminal_id not in terminating
                and attachment is not None
                and attachment.agent_id == agent_id
                and attachment.project_id == project_id
            ):
                self._detach_session(session)
                self._publish_state(session)
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_project_scope(self, project_id: str) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Project."""
        sessions = [
            session
            for session in self._sessions.values()
            if session.lifecycle_owner is not None
            and session.lifecycle_owner.project_id == project_id
        ]
        terminating = {session.terminal_id for session in sessions}
        for session in self._sessions.values():
            attachment = session.attachment
            if (
                session.terminal_id not in terminating
                and attachment is not None
                and attachment.project_id == project_id
            ):
                self._detach_session(session)
                self._publish_state(session)
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    def transfer_scope(self, source: TerminalOwner, target: TerminalOwner) -> int:
        """Transfer lifecycle and attachment scopes after a successful Session move."""
        transferred = 0
        for session in self._sessions.values():
            lifecycle_matches = session.lifecycle_owner == source
            attachment_matches = session.attachment == source
            if lifecycle_matches or attachment_matches:
                attention = session.attention
                pending_delivery = (
                    attachment_matches
                    and attention is not None
                    and session.notification_task is not None
                    and not session.notification_task.done()
                )
                if pending_delivery:
                    self._cancel_delivery(session)
                if lifecycle_matches:
                    session.lifecycle_owner = target
                if attachment_matches:
                    session.attachment = target
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
                    bracketed_paste_was_enabled = session.renderer.bracketed_paste_enabled
                    alternate_screen_exited = session.renderer.feed(text)
                    bracketed_paste_disabled = (
                        bracketed_paste_was_enabled and not session.renderer.bracketed_paste_enabled
                    )
                    title_changed = session.renderer.title != previous_title
                    self._publish_output(session, text)
                    if alternate_screen_exited:
                        self._publish_snapshot(session)
                    if alternate_screen_exited or bracketed_paste_disabled:
                        session.snapshot_on_settle = True
                    state_changed = False
                    if session.state != "starting":
                        state_changed = session.state != "working"
                        session.state = "working"
                        notify = session.attachment is not None and session.settled_delivery_enabled
                        if notify and time.monotonic() < session.resize_grace_until:
                            # Repaint output right after an explicit resize is
                            # viewer noise, not work worth waking the agent for.
                            notify = False
                        self._schedule_settle(session, notify=notify)
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
                session.settled_delivery_enabled = session.attachment is not None
                session.state = "ready"
                if session.snapshot_on_settle:
                    session.snapshot_on_settle = False
                    self._publish_snapshot(session)
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
        command_task = session.operator_command_task
        if (
            command_task is not None
            and command_task is not asyncio.current_task()
            and not command_task.done()
        ):
            command_task.cancel()
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
                self._publish_snapshot(session)
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
                self._publish_snapshot(session)
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
        if self._trigger_service is None or session.attachment is None:
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
        attachment = session.attachment
        if trigger_service is None or attachment is None:
            return
        origin_run_id = session.activity_origin_run_id or session.origin_run_id
        if origin_run_id is None:
            return
        delivery = trigger_service.submit_completion(
            attachment.agent_id,
            attachment.session_id,
            notice_id=attention.notice_id,
            origin_run_id=origin_run_id,
            body=_attention_body(session, attention),
            project_id=attachment.project_id,
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
        command_task = session.operator_command_task
        if (
            command_task is not None
            and command_task is not asyncio.current_task()
            and not command_task.done()
        ):
            command_task.cancel()
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
        attachment = session.attachment
        if task is None or task.done() or attention is None or attachment is None:
            return
        if self._trigger_service is not None:
            self._trigger_service.cancel_completion(
                attachment.agent_id,
                attachment.session_id,
                notice_id=attention.notice_id,
                project_id=attachment.project_id,
            )
        task.cancel()

    def _detach_session(self, session: TerminalSession) -> None:
        """Clear one attachment and any delivery state without touching the child."""
        self._cancel_delivery(session)
        session.attachment = None
        session.activity_origin_run_id = None
        session.notify_on_settle = False
        session.settled_delivery_enabled = False

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
            "workdir": model_path(session.cwd),
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
            "log_file": model_path(session.log_path) if session.log_path is not None else None,
        }

    def _session_group(self, session: TerminalSession) -> TerminalGroup:
        """Return the operator-visible group a Terminal Session belongs to."""
        if session.state in {"exited", "error"} and session.finished_at is not None:
            return self._finished_group()
        group_id = session.group_id
        if group_id is not None and group_id in self._groups:
            return self._groups[group_id]
        if session.owner is None:
            return self._automatic_group(TERMINAL_MANUAL_GROUP_ID)
        return self._automatic_group(agent_group_id(session.owner.agent_id))

    def _group_members(self, group_id: str) -> list[TerminalSession]:
        return [
            session
            for session in self._sessions.values()
            if self._session_group(session).group_id == group_id
        ]

    def _automatic_group(self, group_id: str) -> TerminalGroup:
        name = (
            "Manual"
            if group_id == TERMINAL_MANUAL_GROUP_ID
            else f"Agent {group_id.removeprefix(TERMINAL_AGENT_GROUP_ID_PREFIX)}"
        )
        return TerminalGroup(
            group_id=group_id,
            name=name,
            kind="automatic",
            order=[],
            created_at=_utc_now(),
        )

    def _automatic_group_terminals(self, group_id: str) -> bool:
        return any(
            self._session_group(session).group_id == group_id for session in self._sessions.values()
        )

    def _finished_group(self) -> TerminalGroup:
        return TerminalGroup(
            group_id=TERMINAL_FINISHED_GROUP_ID,
            name="Finished",
            kind="finished",
            order=[],
            created_at=_utc_now(),
        )

    def _finished_group_terminals(self) -> bool:
        return any(session.finished_at is not None for session in self._sessions.values())

    def _distinct_agent_owners(self) -> list[TerminalOwner]:
        owners: dict[tuple[str | None, str], TerminalOwner] = {}
        for session in self._sessions.values():
            owner = session.owner
            if owner is None:
                continue
            owners[(owner.project_id, owner.agent_id)] = owner
        return sorted(
            owners.values(),
            key=lambda owner: (owner.project_id or "", owner.agent_id),
        )

    def _group_summary(self, group: TerminalGroup) -> dict[str, Any]:
        members = self._group_members(group.group_id)
        return {
            "group_id": group.group_id,
            "name": group.name,
            "kind": group.kind,
            "terminal_count": len(members),
            "live_count": sum(session.state not in {"exited", "error"} for session in members),
            "order": list(group.order),
            "source": group.source,
        }

    def _require_group(self, group_id: str) -> TerminalGroup:
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("group_id must be a non-empty string")
        group = self._groups.get(group_id)
        if group is None:
            raise TerminalNotFoundError(f"Terminal group not found: {group_id}")
        return group

    def _group_by_name(self, name: str) -> TerminalGroup | None:
        lowered = name.casefold()
        for group in self._groups.values():
            if group.kind in {"user", "agent"} and group.name.casefold() == lowered:
                return group
        return None

    def _group_name_taken(self, name: str, *, exclude: str | None = None) -> bool:
        lowered = name.casefold()
        return any(
            group.kind in {"user", "agent"}
            and group.group_id != exclude
            and group.name.casefold() == lowered
            for group in self._groups.values()
        )

    def _append_group_terminal(self, group_id: str, terminal_id: str) -> None:
        group = self._groups.get(group_id)
        if group is None or terminal_id in group.order:
            return
        group.order.append(terminal_id)

    def _get_for_operator(self, terminal_id: str) -> TerminalSession:
        session = self._sessions.get(terminal_id)
        if session is None:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return session

    def _operator_summary(self, session: TerminalSession) -> dict[str, Any]:
        attention = session.attention
        owner = session.owner
        attachment = session.attachment
        return {
            "terminal_id": session.terminal_id,
            "group_id": self._session_group(session).group_id,
            "state": session.state,
            "command": Path(session.command).name or session.command,
            "name": session.name,
            "arguments": list(session.arguments),
            "launch_command": session.launch_command,
            "launch_args": list(session.launch_arguments),
            "title": session.renderer.title,
            "workdir": model_path(session.cwd),
            "pid": session.adapter.pid,
            "exit_code": session.exit_code,
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
            "attachment": (
                {
                    "project_id": attachment.project_id,
                    "agent_id": attachment.agent_id,
                    "session_id": attachment.session_id,
                }
                if attachment is not None
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

    def _publish_snapshot(self, session: TerminalSession) -> None:
        session.stream_sequence += 1
        session.stream.publish(
            {
                "type": "terminal_snapshot",
                "sequence": session.stream_sequence,
                "terminal": self._operator_summary(session),
                "ansi": session.renderer.ansi_snapshot(),
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
        owned = sum(1 for session in live if owner is not None and session.lifecycle_owner == owner)
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
    *,
    data: str | None,
    text: str | None,
    key: str | None,
    bracketed_paste: bool = False,
) -> tuple[str, ...]:
    if data == "":
        data = None
    if text == "":
        text = None
    if key == "":
        key = None
    if data is not None:
        if text is not None or key is not None:
            raise ValueError("data cannot be combined with text or key")
        if not data:
            raise ValueError("data must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"data must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        return (data,)
    if key is not None and key not in TERMINAL_INPUT_KEY_SEQUENCES:
        raise ValueError(f"Unsupported terminal key: {key}")
    chunks: list[str] = []
    if text:
        if len(text) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"text must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        chunks.append(
            f"{TERMINAL_BRACKETED_PASTE_START}{text}{TERMINAL_BRACKETED_PASTE_END}"
            if bracketed_paste
            else text
        )
    if key is not None:
        chunks.append(TERMINAL_INPUT_KEY_SEQUENCES[key])
    return tuple(chunks)


def _shell_command(command: str | None, arguments: Sequence[str]) -> str | None:
    """Render one operator-requested command as shell input, or None."""
    if command is None:
        return None
    if not command.strip() or any(not argument for argument in arguments):
        return None
    tokens = [command, *arguments]
    if all(_SHELL_TOKEN_SAFE_RE.fullmatch(token) for token in tokens):
        return " ".join(tokens)
    return " ".join(_shell_quote(token) for token in tokens)


_SHELL_TOKEN_UNQUOTED_RE = re.compile(r"[A-Za-z0-9_\-./:=@+%^,]+")


def _shell_quote(token: str) -> str:
    """Quote one shell word against the host shell's word boundaries.

    Windows shells (PowerShell/cmd) escape a double quote as "" inside
    double quotes; POSIX shells escape it as \\" . Tokens without shell
    metacharacters stay unquoted for readability.
    """
    if _SHELL_TOKEN_SAFE_RE.fullmatch(token):
        return token
    if os.name == "nt":
        if '"' in token:
            token = token.replace('"', '""')
        return f'"{token}"'
    if '"' in token:
        token = token.replace('"', '\\"')
    return f'"{token}"'


_SHELL_TOKEN_SAFE_RE = re.compile(r"[A-Za-z0-9_\-./\\:@=+%,]+")


async def _await_shell_ready(session: TerminalSession) -> bool:
    """Wait for a stable interactive shell prompt screen.

    Returns False when the terminal ends or no prompt can be established
    within the bounded window; the operator command is then never written.
    """
    deadline = time.monotonic() + TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS
    last_revision = -1
    quiet_since: float | None = None
    while True:
        if time.monotonic() >= deadline:
            return False
        async with session.lock:
            if session.state in {"exited", "error"}:
                return False
            revision_now = session.renderer.revision
            text = session.renderer.screen_text()
            has_prompt = bool(text) and _screen_has_prompt_marker(text)
            if has_prompt and revision_now != last_revision:
                last_revision = revision_now
                quiet_since = time.monotonic()
            elif has_prompt and quiet_since is not None:
                if time.monotonic() - quiet_since >= TERMINAL_INITIAL_INPUT_QUIET_SECONDS:
                    break
            elif not has_prompt:
                if revision_now != last_revision:
                    last_revision = revision_now
                quiet_since = None
        session.output_event.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.output_event.wait(), timeout=0.1)
    return True


_SHELL_PROMPT_MARKERS = ("$ ", "# ", "> ", "PS ")
_SHELL_BARE_PROMPT_MARKERS = frozenset(("$", "#", ">", "❯", "➜", "❄", "λ"))


def _screen_has_prompt_marker(text: str) -> bool:
    """Detect a rendered shell prompt in one screen snapshot."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if any(line.startswith(marker) for line in lines for marker in _SHELL_PROMPT_MARKERS):
        return True
    if any(line in _SHELL_BARE_PROMPT_MARKERS for line in lines):
        return True
    return _append_known_prompt_marker(lines[-1])


_KNOWN_PROMPT_PATTERNS = (
    r"[A-Za-z0-9_\-\./\\~]+:\s*$",  # pwsh "PS C:\work> " last line after the chevron
    r"[A-Za-z0-9_\-\./\\~]+(>|#|\$)\s*$",  # cmd "C:\work>", sh "user@host:~/x$"
)


def _append_known_prompt_marker(line: str) -> bool:
    return any(re.search(pattern, line) for pattern in _KNOWN_PROMPT_PATTERNS)


def _parse_launch_history(document: Any) -> list[TerminalLaunchHistoryEntry]:
    if not isinstance(document, dict) or set(document) != {"version", "entries"}:
        raise ValueError("Terminal launch history must contain only version and entries")
    if document["version"] != TERMINAL_LAUNCH_HISTORY_VERSION:
        raise ValueError("Unsupported Terminal launch history version")
    entries = document["entries"]
    if not isinstance(entries, list) or len(entries) > TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES:
        raise ValueError("Terminal launch history entries are invalid")

    parsed: list[TerminalLaunchHistoryEntry] = []
    seen_ids: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "id",
            "command",
            "args",
            "workdir",
            "used_at",
        }:
            raise ValueError("Terminal launch history entry shape is invalid")
        entry_id = raw_entry["id"]
        command = raw_entry["command"]
        arguments = raw_entry["args"]
        workdir = raw_entry["workdir"]
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("Terminal launch history id is invalid")
        if command is not None and (not isinstance(command, str) or not command):
            raise ValueError("Terminal launch history command is invalid")
        if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
            raise ValueError("Terminal launch history arguments are invalid")
        if workdir is not None and (not isinstance(workdir, str) or not workdir):
            raise ValueError("Terminal launch history workdir is invalid")
        if entry_id != _launch_history_id(command, arguments, workdir) or entry_id in seen_ids:
            raise ValueError("Terminal launch history id does not match its configuration")
        used_at = _parse_launch_history_timestamp(raw_entry["used_at"])
        seen_ids.add(entry_id)
        parsed.append(
            TerminalLaunchHistoryEntry(
                id=entry_id,
                command=command,
                arguments=tuple(arguments),
                workdir=workdir,
                used_at=used_at,
            )
        )
    return parsed


def _launch_history_id(
    command: str | None,
    arguments: Sequence[str],
    workdir: str | None,
) -> str:
    encoded = json.dumps(
        {"command": command, "args": list(arguments), "workdir": workdir},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_launch_history_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Terminal launch history timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Terminal launch history timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Terminal launch history timestamp must be UTC")
    return parsed.astimezone(UTC)


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
                "The current screen tail is embedded below so you can usually act "
                "without a follow-up status call. Decide from it whether the program "
                "is still working, is waiting for input, has returned to a prompt, or "
                "needs no action. Use terminal status for the full screen and older "
                "scrollback when needed. Reuse this Terminal Session; do not start a "
                "duplicate process.",
                "",
                "```",
                session.renderer.screen_tail(TERMINAL_DELIVERY_TAIL_LINES),
                "```",
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


def _validate_group_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Terminal group name must not be empty")
    if len(name.strip()) > TERMINAL_GROUP_NAME_MAX_CHARS:
        raise ValueError(
            f"Terminal group name must be at most {TERMINAL_GROUP_NAME_MAX_CHARS} characters"
        )
    return name.strip()


def agent_group_id(agent_id: str) -> str:
    """Return the automatic group id for one Agent owner."""
    return f"{TERMINAL_AGENT_GROUP_ID_PREFIX}{agent_id}"


def _parse_groups(document: Any) -> list[TerminalGroup]:
    if not isinstance(document, dict) or set(document) != {"version", "groups"}:
        raise ValueError("Terminal groups must contain only version and groups")
    if document["version"] != TERMINAL_GROUPS_VERSION:
        raise ValueError("Unsupported Terminal groups version")
    raw_groups = document["groups"]
    if not isinstance(raw_groups, list):
        raise ValueError("Terminal groups entries are invalid")

    parsed: list[TerminalGroup] = []
    seen_ids: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict) or set(raw_group) != {
            "id",
            "name",
            "order",
            "created_at",
        }:
            raise ValueError("Terminal group shape is invalid")
        group_id = raw_group["id"]
        name = raw_group["name"]
        order = raw_group["order"]
        created_at = raw_group["created_at"]
        if not isinstance(group_id, str) or not group_id or group_id in seen_ids:
            raise ValueError("Terminal group id is invalid or duplicated")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Terminal group name is invalid")
        if not isinstance(order, list) or any(
            not isinstance(item, str) or not item for item in order
        ):
            raise ValueError("Terminal group order is invalid")
        parsed_at = _parse_group_timestamp(created_at)
        seen_ids.add(group_id)
        parsed.append(
            TerminalGroup(
                group_id=group_id,
                name=name.strip(),
                kind="user",
                order=list(order),
                created_at=parsed_at,
            )
        )
    return parsed


def _parse_group_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Terminal group timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Terminal group timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Terminal group timestamp must be UTC")
    return parsed.astimezone(UTC)


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
    "GroupKind",
    "TERMINAL_AGENT_GROUP_ID_PREFIX",
    "TERMINAL_DEFAULT_COLUMNS",
    "TERMINAL_DEFAULT_ROWS",
    "TERMINAL_FINISHED_GROUP_ID",
    "TERMINAL_FINISHED_TTL",
    "TERMINAL_GROUP_NAME_MAX_CHARS",
    "TERMINAL_INPUT_MAX_CHARS",
    "TERMINAL_INPUT_KEY_SEQUENCES",
    "TERMINAL_MANUAL_GROUP_ID",
    "TERMINAL_MAX_COLUMNS",
    "TERMINAL_MAX_ROWS",
    "TERMINAL_MIN_COLUMNS",
    "TERMINAL_MIN_ROWS",
    "TERMINAL_STATUS_DEFAULT_LINES",
    "TERMINAL_STATUS_MAX_LINES",
    "TERMINAL_TEMPORARY_CATEGORY",
    "TerminalAdapter",
    "TerminalAlreadyAttachedError",
    "TerminalCapacityError",
    "TerminalClosedError",
    "TerminalCursorError",
    "TerminalGroup",
    "TerminalLaunchError",
    "TerminalLaunchHistoryEntry",
    "TerminalManager",
    "TerminalManagerError",
    "TerminalNotFoundError",
    "TerminalNotAttachedError",
    "TerminalOwner",
    "TerminalSession",
    "TerminalStaleScreenError",
    "TerminalState",
    "agent_group_id",
]
