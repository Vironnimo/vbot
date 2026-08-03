"""Session-scoped interactive PTY/ConPTY lifecycle and attention management."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import tempfile
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.tools.terminal_backend import (
    TerminalAdapter,
    TerminalAdapterFactory,
    TerminalRenderer,
    is_codex_executable,
    prepare_codex_launch,
    spawn_terminal_adapter,
    terminate_process_tree,
)
from core.tools.terminal_hook_sink import (
    TERMINAL_HOOK_EVENT_VERSION,
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
TERMINAL_EVENT_POLL_SECONDS = 0.1
TERMINAL_SWEEP_INTERVAL_SECONDS = 60.0
TERMINAL_FINISHED_TTL = timedelta(minutes=30)
TERMINAL_NOTICE_MESSAGE_CAP_CHARS = 16_000
TERMINAL_CURSOR_VERSION = 1
TERMINAL_TEMPORARY_CATEGORY = "terminals"
TERMINAL_INITIAL_INPUT_QUIET_SECONDS = 0.5
TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS = 15.0
TERMINAL_INPUT_KEY_DELAY_SECONDS = 0.1
_CODEX_QUESTION_TOOL = "request_user_input"

TerminalState = Literal[
    "starting",
    "ready",
    "working",
    "needs_input",
    "turn_complete",
    "exited",
    "error",
]
AttentionKind = Literal["approval", "question", "turn_complete", "exited", "error"]


class TerminalManagerError(VBotError):
    """Base class for expected Terminal Manager failures."""


class TerminalNotFoundError(TerminalManagerError):
    """Raised when a Terminal Session is missing or belongs to another Session."""


class TerminalClosedError(TerminalManagerError):
    """Raised when input or resize targets a closed Terminal Session."""


class TerminalCapacityError(TerminalManagerError):
    """Raised when a live Terminal Session capacity limit is reached."""


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
    """One structured Agent-attention boundary for a Terminal Session."""

    revision: int
    kind: AttentionKind
    notice_id: str
    turn_id: str | None
    summary: str
    details: dict[str, Any]
    created_at: datetime
    delivered: bool = False


@dataclass(slots=True)
class TerminalSession:
    """In-memory state for one interactive terminal process."""

    terminal_id: str
    owner: TerminalOwner
    adapter: TerminalAdapter
    renderer: TerminalRenderer
    command: str
    arguments: tuple[str, ...]
    cwd: Path
    state: TerminalState
    started_at: datetime
    origin_run_id: str
    turn_origin_run_id: str | None
    codex_integration: bool
    event_nonce: str | None
    event_path: Path | None
    event_lease: TemporaryFileLease | None
    delete_event_on_finish: bool
    log_path: Path | None
    log_handle: TextIO | None
    log_lease: TemporaryFileLease | None
    exit_code: int | None = None
    finished_at: datetime | None = None
    external_session_id: str | None = None
    external_turn_id: str | None = None
    attention_revision: int = 0
    acknowledged_attention_revision: int = 0
    attention: TerminalAttention | None = None
    event_offset: int = 0
    event_remainder: bytes = b""
    seen_event_keys: deque[str] = field(default_factory=lambda: deque(maxlen=128))
    suppress_exit_attention: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    output_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    attention_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    event_task: asyncio.Task[None] | None = field(default=None, repr=False)
    initial_input_task: asyncio.Task[None] | None = field(default=None, repr=False)
    notification_task: asyncio.Task[None] | None = field(default=None, repr=False)


class TerminalManager:
    """Own interactive terminal processes across Runs within one vBot Session."""

    def __init__(
        self,
        trigger_service: Any | None = None,
        *,
        temporary_files: TemporaryFileManager | None = None,
        adapter_factory: TerminalAdapterFactory | None = None,
        scrollback_lines: int = TERMINAL_SCROLLBACK_LINES,
        finished_session_ttl: timedelta = TERMINAL_FINISHED_TTL,
        sweep_interval_seconds: float = TERMINAL_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if scrollback_lines < 1:
            raise ValueError("Terminal scrollback cap must be positive")
        if finished_session_ttl <= timedelta(0):
            raise ValueError("Terminal finished-session TTL must be positive")
        if sweep_interval_seconds <= 0:
            raise ValueError("Terminal sweep interval must be positive")
        self._trigger_service = trigger_service
        self._temporary_files = temporary_files
        self._adapter_factory = adapter_factory or spawn_terminal_adapter
        self._scrollback_lines = scrollback_lines
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sessions: dict[str, TerminalSession] = {}
        self._cursor_secret = os.urandom(32)
        self._sweeper_task: asyncio.Task[None] | None = None

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
            for task in (session.reader_task, session.event_task, session.initial_input_task):
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
                session.event_task,
                session.initial_input_task,
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
        """Start one PTY/ConPTY child and optionally submit its first turn."""
        _validate_owner(owner)
        if not argv or not argv[0]:
            raise ValueError("Terminal command must not be empty")
        _validate_dimensions(columns, rows)
        if not cwd.is_dir():
            raise ValueError(f"Terminal workdir is not a directory: {cwd}")
        self._enforce_capacity(owner)

        terminal_id = uuid.uuid4().hex
        codex_integration = is_codex_executable(argv[0])
        event_path: Path | None = None
        event_lease: TemporaryFileLease | None = None
        delete_event_on_finish = False
        event_nonce: str | None = None
        log_path: Path | None = None
        log_handle: TextIO | None = None
        log_lease: TemporaryFileLease | None = None
        process_env = dict(os.environ)
        if env is not None:
            process_env.update(env)
        process_env.setdefault("TERM", "xterm-256color")
        process_env["PYTHONIOENCODING"] = "utf-8"

        try:
            log_path, log_handle, log_lease = self._open_raw_log()
            if codex_integration:
                event_path, event_lease, delete_event_on_finish = self._create_event_file()
                event_nonce = uuid.uuid4().hex
                prepared_argv = prepare_codex_launch(argv, process_env, event_path, event_nonce)
            else:
                prepared_argv = list(argv)
            adapter = await asyncio.to_thread(
                self._adapter_factory,
                prepared_argv,
                cwd,
                process_env,
                rows,
                columns,
            )
        except Exception:
            if log_handle is not None:
                log_handle.close()
            if log_lease is not None:
                log_lease.finish()
            if event_lease is not None:
                event_lease.finish()
            if delete_event_on_finish and event_path is not None:
                event_path.unlink(missing_ok=True)
            raise

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
            turn_origin_run_id=None,
            codex_integration=codex_integration,
            event_nonce=event_nonce,
            event_path=event_path,
            event_lease=event_lease,
            delete_event_on_finish=delete_event_on_finish,
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
        if event_path is not None:
            session.event_task = asyncio.create_task(
                self._read_hook_events(session), name=f"terminal:{terminal_id}:events"
            )
            session.event_task.add_done_callback(
                lambda task: _log_background_task_result(
                    task, f"Terminal event reader failed for terminal={terminal_id}"
                )
            )
        if initial_text is not None:
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
    ) -> dict[str, Any]:
        """Write terminal text/keys and update the projected Codex turn state."""
        session = self.get_session(terminal_id, owner)
        chunks = _input_chunks(text=text, key=key, enter=enter)
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
            if prior_state in {"ready", "turn_complete"}:
                session.turn_origin_run_id = origin_run_id
                session.external_turn_id = None
            session.state = "working"
            for index, chunk in enumerate(chunks):
                if index:
                    await asyncio.sleep(TERMINAL_INPUT_KEY_DELAY_SECONDS)
                await asyncio.to_thread(session.adapter.write, chunk)
            session.output_event.set()
            return {
                "terminal_id": terminal_id,
                "state": session.state,
                "characters_sent": len(text or ""),
                "key": key,
                "enter": enter,
                "answered_attention_revision": (
                    prior_attention_revision if prior_state == "needs_input" else None
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
            await self.send_input(
                session.terminal_id,
                session.owner,
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
        _validate_dimensions(columns, rows)
        session = self.get_session(terminal_id, owner)
        async with session.lock:
            self._require_live(session)
            await asyncio.to_thread(session.adapter.resize, rows, columns)
            session.renderer.resize(columns, rows)
            return {
                "terminal_id": terminal_id,
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
            if session.owner.agent_id == agent_id and session.owner.project_id == project_id
        ]
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_project_scope(self, project_id: str) -> None:
        """Terminate every Terminal Session under a removed Project."""
        sessions = [
            session for session in self._sessions.values() if session.owner.project_id == project_id
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
                    session.renderer.feed(text)
                    session.output_event.set()
        except asyncio.CancelledError:
            raise
        except (EOFError, OSError):
            pass
        except BaseException as caught:
            error = caught
        finally:
            await self._mark_finished(session, error)

    async def _read_hook_events(self, session: TerminalSession) -> None:
        assert session.event_path is not None
        try:
            while session.state not in {"exited", "error"}:
                await asyncio.sleep(TERMINAL_EVENT_POLL_SECONDS)
                data = await asyncio.to_thread(
                    _read_file_from_offset, session.event_path, session.event_offset
                )
                if not data:
                    continue
                session.event_offset += len(data)
                combined = session.event_remainder + data
                lines = combined.split(b"\n")
                session.event_remainder = lines.pop()
                for line in lines:
                    if line:
                        await self._consume_hook_record(session, line)
        except asyncio.CancelledError:
            return

    async def _consume_hook_record(self, session: TerminalSession, line: bytes) -> None:
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return
        if (
            not isinstance(record, dict)
            or record.get("version") != TERMINAL_HOOK_EVENT_VERSION
            or record.get("nonce") != session.event_nonce
            or not isinstance(record.get("event"), dict)
        ):
            return
        event = cast(dict[str, Any], record["event"])
        event_key = _hook_event_key(event)
        if event_key in session.seen_event_keys:
            return
        session.seen_event_keys.append(event_key)
        external_session_id = _optional_nonblank_string(event.get("session_id"))
        if session.external_session_id is None:
            session.external_session_id = external_session_id
        elif external_session_id != session.external_session_id:
            return
        hook_name = event.get("hook_event_name")
        turn_id = _optional_nonblank_string(event.get("turn_id"))
        async with session.lock:
            if session.state in {"exited", "error"}:
                return
            session.external_turn_id = turn_id
            if hook_name == "PermissionRequest":
                tool_name = _optional_nonblank_string(event.get("tool_name"))
                tool_input = event.get("tool_input")
                details = {
                    "tool_name": tool_name,
                    "tool_input": tool_input if _is_json_value(tool_input) else None,
                }
                session.state = "needs_input"
                self._set_attention(
                    session,
                    kind="approval",
                    turn_id=turn_id,
                    summary=f"Codex requests approval for {tool_name or 'a tool'}.",
                    details=details,
                )
                return
            if hook_name == "PreToolUse" and event.get("tool_name") == _CODEX_QUESTION_TOOL:
                tool_input = event.get("tool_input")
                details = {
                    "questions": (
                        tool_input.get("questions")
                        if isinstance(tool_input, dict)
                        and _is_json_value(tool_input.get("questions"))
                        else None
                    )
                }
                session.state = "needs_input"
                self._set_attention(
                    session,
                    kind="question",
                    turn_id=turn_id,
                    summary="Codex is waiting for an answer to a structured question.",
                    details=details,
                )
                return
            if hook_name == "Stop":
                message = event.get("last_assistant_message")
                final_message = message if isinstance(message, str) else ""
                session.state = "turn_complete"
                self._set_attention(
                    session,
                    kind="turn_complete",
                    turn_id=turn_id,
                    summary="Codex completed its current turn.",
                    details={"final_message": final_message},
                )

    async def _mark_finished(self, session: TerminalSession, error: BaseException | None) -> None:
        initial_task = session.initial_input_task
        if (
            initial_task is not None
            and initial_task is not asyncio.current_task()
            and not initial_task.done()
        ):
            initial_task.cancel()
        async with session.lock:
            if session.finished_at is not None:
                return
            session.finished_at = _utc_now()
            session.exit_code = await asyncio.to_thread(session.adapter.exit_code)
            if error is None:
                session.state = "exited"
                if not session.suppress_exit_attention:
                    self._set_attention(
                        session,
                        kind="exited",
                        turn_id=session.external_turn_id,
                        summary=f"Terminal process exited with code {session.exit_code}.",
                        details={"exit_code": session.exit_code},
                    )
            else:
                session.state = "error"
                if not session.suppress_exit_attention:
                    self._set_attention(
                        session,
                        kind="error",
                        turn_id=session.external_turn_id,
                        summary="Terminal transport or rendering failed.",
                        details={"error": str(error)},
                    )
            session.attention_event.set()
            session.output_event.set()
            self._finish_files(session)
        event_task = session.event_task
        if (
            event_task is not None
            and event_task is not asyncio.current_task()
            and not event_task.done()
        ):
            event_task.cancel()

    def _set_attention(
        self,
        session: TerminalSession,
        *,
        kind: AttentionKind,
        turn_id: str | None,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        self._cancel_delivery(session)
        session.attention_revision += 1
        revision = session.attention_revision
        attention = TerminalAttention(
            revision=revision,
            kind=kind,
            notice_id=f"terminal:{session.terminal_id}:attention:{revision}",
            turn_id=turn_id,
            summary=summary,
            details=details,
            created_at=_utc_now(),
        )
        session.attention = attention
        session.attention_event.set()
        self._schedule_attention_delivery(session, attention)

    def _schedule_attention_delivery(
        self, session: TerminalSession, attention: TerminalAttention
    ) -> None:
        if self._trigger_service is None:
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
        if trigger_service is None:
            return
        origin_run_id = session.turn_origin_run_id or session.origin_run_id
        delivery = trigger_service.submit_completion(
            session.owner.agent_id,
            session.owner.session_id,
            notice_id=attention.notice_id,
            origin_run_id=origin_run_id,
            body=_attention_body(session, attention),
            project_id=session.owner.project_id,
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
        if session.state not in {"exited", "error"}:
            await asyncio.to_thread(terminate_process_tree, session.adapter)
        reader = session.reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=5)
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        if session.finished_at is None:
            await self._mark_finished(session, None)

    def _cancel_delivery(self, session: TerminalSession) -> None:
        task = session.notification_task
        attention = session.attention
        if task is None or task.done() or attention is None:
            return
        if self._trigger_service is not None:
            self._trigger_service.cancel_completion(
                session.owner.agent_id,
                session.owner.session_id,
                notice_id=attention.notice_id,
                project_id=session.owner.project_id,
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
            "integration": (
                {
                    "kind": "codex_hooks",
                    "structured_attention": ["approval", "question", "turn_complete"],
                }
                if session.codex_integration
                else None
            ),
            "log_file": str(session.log_path) if session.log_path is not None else None,
        }

    def _enforce_capacity(self, owner: TerminalOwner) -> None:
        live = [
            session
            for session in self._sessions.values()
            if session.state not in {"exited", "error"}
        ]
        if len(live) >= TERMINAL_MAX_LIVE_GLOBAL:
            raise TerminalCapacityError(
                f"Live Terminal Session limit reached ({TERMINAL_MAX_LIVE_GLOBAL})"
            )
        owned = sum(1 for session in live if session.owner == owner)
        if owned >= TERMINAL_MAX_LIVE_PER_SESSION:
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

    def _create_event_file(self) -> tuple[Path, TemporaryFileLease | None, bool]:
        if self._temporary_files is not None:
            lease = self._temporary_files.create(TERMINAL_TEMPORARY_CATEGORY, ".events.jsonl")
            return lease.path, lease, False
        descriptor, raw_path = tempfile.mkstemp(prefix="vbot-terminal-", suffix=".events.jsonl")
        os.close(descriptor)
        return Path(raw_path), None, True

    @staticmethod
    def _finish_files(session: TerminalSession) -> None:
        if session.log_handle is not None:
            with contextlib.suppress(OSError):
                session.log_handle.close()
            session.log_handle = None
        if session.log_lease is not None:
            session.log_lease.finish()
            session.log_lease = None
        if session.event_lease is not None:
            session.event_lease.finish()
            session.event_lease = None
        if session.delete_event_on_finish and session.event_path is not None:
            with contextlib.suppress(OSError):
                session.event_path.unlink(missing_ok=True)
            session.delete_event_on_finish = False

    @staticmethod
    def _require_live(session: TerminalSession) -> None:
        if session.state in {"exited", "error"}:
            raise TerminalClosedError("Terminal Session is no longer running")

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return


def _input_chunks(*, text: str | None, key: str | None, enter: bool) -> tuple[str, ...]:
    key_sequences = {
        "enter": "\r",
        "escape": "\x1b",
        "ctrl_c": "\x03",
        "ctrl_d": "\x04",
        "tab": "\t",
        "backspace": "\x7f",
        "up": "\x1b[A",
        "down": "\x1b[B",
        "right": "\x1b[C",
        "left": "\x1b[D",
    }
    if key is not None and key not in key_sequences:
        raise ValueError(f"Unsupported terminal key: {key}")
    chunks: list[str] = []
    if text:
        chunks.append(text)
    if key is not None:
        chunks.append(key_sequences[key])
    if enter:
        chunks.append("\r")
    if not chunks:
        raise ValueError("input must send text, a key, or Enter")
    return tuple(chunks)


def _attention_body(session: TerminalSession, attention: TerminalAttention) -> str:
    heading = {
        "approval": "Codex approval required",
        "question": "Codex question requires an answer",
        "turn_complete": "Codex turn complete",
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
    if attention.turn_id:
        sections.append(f"Codex turn id: {attention.turn_id}")
    if attention.kind == "turn_complete":
        final_message = attention.details.get("final_message")
        if isinstance(final_message, str) and final_message:
            sections.extend(("Codex final response:", final_message))
        sections.append(
            "The Terminal Session remains open for later tasks. Re-evaluate the original "
            "user goal and current workspace state before deciding whether more work is "
            "required."
        )
    elif attention.kind in {"approval", "question"}:
        sections.extend(
            (
                "Structured request:",
                json.dumps(attention.details, ensure_ascii=False, indent=2),
                "Use terminal_beta status if more screen context is needed, then answer with "
                "terminal_beta input or ask the user. Do not start another Codex process.",
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
        "turn_id": attention.turn_id,
        "summary": attention.summary,
        "details": attention.details,
        "created_at": attention.created_at.isoformat(),
        "delivered": attention.delivered,
    }


def _hook_event_key(event: Mapping[str, Any]) -> str:
    stable = {
        "hook": event.get("hook_event_name"),
        "turn": event.get("turn_id"),
        "tool_use": event.get("tool_use_id"),
        "tool": event.get("tool_name"),
        "message": event.get("last_assistant_message"),
    }
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _read_file_from_offset(path: Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read()


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _optional_nonblank_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


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
    "TerminalManager",
    "TerminalManagerError",
    "TerminalNotFoundError",
    "TerminalOwner",
    "TerminalSession",
    "TerminalStaleScreenError",
    "TerminalState",
]
