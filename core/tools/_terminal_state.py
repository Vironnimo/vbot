"""Terminal Session values, limits and errors."""

from __future__ import annotations

from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from core.utils.log_conditions import LoggedConditions

if TYPE_CHECKING:
    from ._command_types import CompletionSink
    from ._terminal_render_host import TerminalScreen


import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.event_stream import ReplayEventStream
from core.sessions import SessionAddress
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.terminal_manager")
_CONDITIONS = LoggedConditions()

TERMINAL_DEFAULT_COLUMNS = 80
TERMINAL_DEFAULT_ROWS = 24
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
# A message that brings a screen holds at most this many characters; screen
# rows beyond it are left out from the top, and the message says how many.
TERMINAL_NOTICE_MESSAGE_CAP_CHARS = 16_000
TERMINAL_TEMPORARY_CATEGORY = "terminals"
# Output paused this long after input or start ends the reply a Tool call shows,
# and, with the evidence that the work ended, the cycle (``TerminalActivity``).
TERMINAL_ACTIVITY_QUIET_SECONDS = 2.0
# The fallback idle boundary: no output for this long, with a quiet process
# tree, ends a cycle whose program neither declared its end nor showed it.
# Working programs pause between progress updates for several seconds (up to
# 6 s measured); waking at every 2 s pause woke Agents 17 times in 63 s while
# their program kept working.
TERMINAL_IDLE_SECONDS = 15.0
# An Agent start returns once the program showed something and its output
# then paused for the quiet period, or once the program ended; at the latest
# after this many seconds. A terminal's own set-up sequences show nothing.
TERMINAL_START_WAIT_SECONDS = 10.0
# Output this soon after resizing a quiet terminal is the program redrawing
# its screen for the new size. Claude Code, Codex and OpenCode finish that
# redraw within about 15-125 ms, in one burst.
TERMINAL_REPAINT_WINDOW_SECONDS = 1.0
TERMINAL_STREAM_RETENTION_EVENTS = 4_096
TERMINAL_STREAM_BYTE_LIMIT = 4 * 1024 * 1024
TERMINAL_STREAM_SUBSCRIBER_QUEUE_EVENTS = 512
TERMINAL_INPUT_MAX_CHARS = 65_536
TERMINAL_BRACKETED_PASTE_START = "\x1b[200~"
TERMINAL_BRACKETED_PASTE_END = "\x1b[201~"
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
    "ready",
    "working",
    "exited",
    "error",
]
AttentionKind = Literal["idle", "exited", "error"]
# Why a wait for a program ended: it exited, printed output matching the
# pattern, went quiet after input, went idle showing a new screen, or the
# wait's time ran out.
WaitEnded = Literal["exited", "matched", "quiet", "idle", "timeout"]
TerminalStreamEvent = dict[str, Any]
TerminalChangedCallback = Callable[[str], None]


def _stream_event_size(value: Any) -> int:
    """Conservative retained Python payload size, including Unicode storage."""
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(
            _stream_event_size(key) + _stream_event_size(item) for key, item in value.items()
        )
    elif isinstance(value, (list, tuple)):
        size += sum(_stream_event_size(item) for item in value)
    return size


def _new_terminal_stream() -> ReplayEventStream[TerminalStreamEvent]:
    return ReplayEventStream(
        event_retention_limit=TERMINAL_STREAM_RETENTION_EVENTS,
        subscriber_queue_limit=TERMINAL_STREAM_SUBSCRIBER_QUEUE_EVENTS,
        byte_limit=TERMINAL_STREAM_BYTE_LIMIT,
        size_of=_stream_event_size,
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
    """Raised when a Terminal Session does not exist."""


class TerminalAlreadyAttachedError(TerminalManagerError):
    """Raised when another vBot Session already holds the one attachment."""


class TerminalNotAttachedError(TerminalManagerError):
    """Raised when detach does not target the current attachment."""


class TerminalNotOwnedError(TerminalManagerError):
    """Raised when a Terminal Session exists but is not attached to the calling Session.

    Discovery (``list``) grants no operational access. Controlling, status,
    or targeting a Terminal Session requires the exact ``attach`` binding, so
    the caller must ``attach`` the Session to its own Session first.
    """


class TerminalClosedError(TerminalManagerError):
    """Raised when input or resize targets a closed Terminal Session."""


class TerminalCapacityError(TerminalManagerError):
    """Raised when a live Terminal Session capacity limit is reached.

    ``scope`` names the limit: ``session`` (one vBot Session's terminals) or
    ``global`` (every terminal). ``terminals`` are the live terminals the
    requesting Session started, oldest first: the ones it can stop.
    """

    def __init__(
        self,
        message: str,
        *,
        scope: Literal["session", "global"],
        limit: int,
        terminals: tuple[TerminalInfo, ...] = (),
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.limit = limit
        self.terminals = terminals


class TerminalLaunchError(TerminalManagerError):
    """Raised when the host cannot start a requested terminal process."""


class TerminalStaleScreenError(TerminalManagerError):
    """Raised when input was based on an obsolete rendered screen."""


class TerminalProgramNotRunningError(TerminalManagerError):
    """Raised when input expected a program that does not run in the Terminal."""


@dataclass(frozen=True, slots=True)
class TerminalOwner:
    """Exact vBot Session address used by Terminal lifecycle and attachment scopes."""

    project_id: str | None
    agent_id: str
    session_id: str

    @property
    def address(self) -> SessionAddress:
        return SessionAddress(self.project_id, self.agent_id, self.session_id)


@dataclass(frozen=True, slots=True)
class TerminalSessionView:
    """The conversation a Terminal is shown under: the root Session of a
    Sub-Agent tree, with its title (``None`` before it has one)."""

    root: TerminalOwner
    title: str | None


@dataclass(frozen=True, slots=True)
class TerminalAttention:
    """One program-agnostic Agent-attention boundary for a Terminal Session."""

    revision: int
    kind: AttentionKind
    notice_id: str
    summary: str
    details: dict[str, Any]
    created_at: datetime
    delivered: bool = False


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    """Immutable view of one Terminal Session at the moment it was taken."""

    terminal_id: str
    # Process provenance. None means the local operator started it.
    owner: TerminalOwner | None
    lifecycle_owner: TerminalOwner | None
    attachment: TerminalOwner | None
    state: TerminalState
    command: str
    arguments: tuple[str, ...]
    launch_command: str | None
    launch_arguments: tuple[str, ...]
    # A short first name, unique among retained Terminals, that the user calls it by.
    call_name: str
    group_id: str | None
    cwd: Path
    pid: int
    started_at: datetime
    finished_at: datetime | None
    exit_code: int | None
    title: str
    columns: int
    rows: int
    alternate_screen: bool
    screen_revision: int
    attention: TerminalAttention | None
    attention_revision: int
    acknowledged_attention_revision: int
    log_path: Path | None
    # vBot, the user or an Agent stopped the program before it ended on its own.
    stopped: bool = False

    @property
    def finished(self) -> bool:
        return self.state in {"exited", "error"}

    @property
    def program(self) -> str:
        """The program as it was asked for: the program a manual terminal runs
        inside its shell, or the started command line."""
        if self.launch_command:
            return _command_line((self.launch_command, *self.launch_arguments))
        return _command_line((self.command, *self.arguments))


def _command_line(words: tuple[str, ...]) -> str:
    """Join words for reading, quoting the ones a space would split."""
    return " ".join(
        f'"{word}"' if not word or any(character.isspace() for character in word) else word
        for word in words
    )


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


@dataclass(frozen=True, slots=True)
class TerminalSessionServices:
    """What every Terminal Session of one manager shares."""

    trigger_service: CompletionSink | None
    reader_executor: ThreadPoolExecutor
    activity_quiet_seconds: float
    idle_seconds: float
    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    program_probe: Callable[[int, str], bool]
    # The operator summary of a Terminal (owned by the catalog, which knows groups).
    operator_summary: Callable[[TerminalInfo], dict[str, Any]]
    # Operator-visible Terminal state changed.
    changed: Callable[[str], None]
    # A new screen of this size, for one drawn anew from the log.
    open_screen: Callable[[int, int], Awaitable[TerminalScreen]]


@dataclass(frozen=True, slots=True)
class TerminalObservation:
    """The screen a screen-bearing result showed, remembered once it is durable."""

    screen_revision: int
    resize_count: int
    columns: int
    rows: int
    # Visible-cell signature: the Agent saw this screen, so its idle boundary wakes nobody.
    signature: str
    # The history line number just below the last screen line shown: the
    # Agent has seen every line above it that scrolls into history.
    seen_line: int
    # Clears of the screen before it was shown; a later clear erased what it
    # showed from the clear's first line on.
    clears: int = 0


@dataclass(frozen=True, slots=True)
class TerminalLaunch:
    """How the Session's process was started, as callers see it."""

    command: str
    arguments: tuple[str, ...]
    cwd: Path
    # A manual start that runs a program inside the interactive shell.
    launch_command: str | None = None
    launch_arguments: tuple[str, ...] = ()


def _log_task_failure(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled() or (error := task.exception()) is None:
        return
    _LOGGER.error("%s: %s", message, error, exc_info=(type(error), error, error.__traceback__))
