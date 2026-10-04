"""Terminal Session values, limits and errors."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.event_stream import ReplayEventStream
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.terminal_manager")

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
TERMINAL_NOTICE_MESSAGE_CAP_CHARS = 16_000
# Embed enough recent screen rows for the notified Agent to act directly.
TERMINAL_DELIVERY_TAIL_LINES = 20
TERMINAL_TEMPORARY_CATEGORY = "terminals"
TERMINAL_INITIAL_INPUT_QUIET_SECONDS = 0.5
TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS = 15.0
TERMINAL_ACTIVITY_QUIET_SECONDS = 2.0
# Output this soon after resizing a quiet terminal is the program redrawing
# its screen for the new size. Claude Code, Codex and OpenCode finish that
# redraw within about 15-125 ms, in one burst.
TERMINAL_REPAINT_WINDOW_SECONDS = 1.0
TERMINAL_INPUT_KEY_DELAY_SECONDS = 0.1
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
    "starting",
    "ready",
    "working",
    "exited",
    "error",
]
AttentionKind = Literal["output_settled", "exited", "error"]
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
    """Raised when a live Terminal Session capacity limit is reached."""


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
    name: str | None
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

    @property
    def finished(self) -> bool:
        return self.state in {"exited", "error"}


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
