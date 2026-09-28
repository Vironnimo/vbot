"""How the fake Provider scripts Swarm participants (``--scenario swarm``).

A Swarm participant's Session never carries the goal in a User message: its
first Model request only asks it to read the goal post on the Board, and later
Runs start from wake notes and delivered Board messages. For a request that
offers ``swarm_board`` the fake Provider therefore

- answers a history without any ``[[perf ...]]`` directive with the
  ``swarm_board`` read of the goal post named in the initial message;
- otherwise takes the first directive anywhere in the history (the goal post in
  that read's Tool result) and derives the participant's position from the
  history alone: completed turns are the scripted final texts after the
  directive, the current turn's Tool rounds are the Tool-call responses after
  the latest final text.

A participant whose ``turns`` budget is spent, and every participant while the
harness winds the load down, answers the first request of a new turn with a
short plain text and no Tool call. Swarm wake pacing then keeps it quiet until
someone addresses it or the user posts. The participant key ``p<N>`` is the
number of the goal read's Tool-call id, which the fake Provider issues once per
participant, so the key is stable for the participant's whole history.

Everything here is a pure function of the request, so the fake Provider keeps
no per-participant state.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scripts.perf_load_suite.directive import PerfDirective, find_directive, message_text

SWARM_BOARD_TOOL = "swarm_board"
DEFAULT_SWARM_TOOLS: tuple[str, ...] = ("swarm_board.post", "read", "swarm_board.read")
IDLE_RESPONSE_TEXT = "Nothing further from me right now."
DEFAULT_GOAL_POST = "#0"
POST_WORDS = 24
BOARD_READ_LIMIT = 10
_GOAL_POST_PATTERN = re.compile(r'"message_id":\s*"(#\d+)"')
_CALL_ID_PATTERN = re.compile(r"^call_perf_(\d+)$")
_WORDS = (
    "swarm",
    "board",
    "note",
    "plan",
    "review",
    "check",
    "draft",
    "status",
    "detail",
    "update",
    "result",
    "step",
)
# Tool -> actions the script can call (first = default without a suffix).
SWARM_ACTIONS: dict[str, tuple[str, ...]] = {
    "swarm_board": ("post", "read"),
    "swarm_wiki": ("create", "list"),
    "swarm_state": ("status",),
}


class SwarmScriptError(ValueError):
    """A Swarm participant request cannot be answered as scripted."""


@dataclass(frozen=True)
class SwarmPosition:
    """Where one participant stands in its scripted turns."""

    participant: str
    directive: PerfDirective
    completed_turns: int
    rounds_in_turn: int

    @property
    def tag(self) -> str:
        return turn_tag(self.participant, self.completed_turns + 1)

    @property
    def round_index(self) -> int:
        """Request index within the turn; the goal read is round 0 of turn 1."""
        return self.rounds_in_turn + (1 if self.completed_turns == 0 else 0)

    @property
    def starts_turn(self) -> bool:
        return self.rounds_in_turn == 0

    @property
    def budget_spent(self) -> bool:
        return 0 < self.directive.turns <= self.completed_turns


def turn_tag(participant: str, turn: int) -> str:
    return f"{participant}-t{turn}"


def participant_for_call_id(call_id: str) -> str | None:
    """Participant key for the goal read issued under ``call_id``."""
    match = _CALL_ID_PATTERN.match(call_id)
    return f"p{match.group(1)}" if match else None


def goal_read_arguments(messages: Sequence[Any]) -> dict[str, Any]:
    """Read the goal post the participant's initial message names."""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            match = _GOAL_POST_PATTERN.search(message_text(message))
            if match:
                return {"action": "read", "message_id": match.group(1)}
    return {"action": "read", "message_id": DEFAULT_GOAL_POST}


def swarm_position(messages: Sequence[Any]) -> SwarmPosition | None:
    """The participant's position, or ``None`` before it read the goal.

    Raises :class:`DirectiveError` for a malformed directive.
    """
    located = _first_directive(messages)
    if located is None:
        return None
    directive, index = located
    completed = 0
    rounds = 0
    for message in messages[index + 1 :]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("tool_calls"):
            rounds += 1
            continue
        rounds = 0
        if not message_text(message).startswith(IDLE_RESPONSE_TEXT):
            completed += 1
    return SwarmPosition(
        participant=_participant(messages[index]),
        directive=directive,
        completed_turns=completed,
        rounds_in_turn=rounds,
    )


def _first_directive(messages: Sequence[Any]) -> tuple[PerfDirective, int] | None:
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in {"user", "tool"}:
            continue
        directive = find_directive(message_text(message))
        if directive is not None:
            return directive, index
    return None


def _participant(message: dict[str, Any]) -> str:
    call_id = message.get("tool_call_id")
    if isinstance(call_id, str):
        participant = participant_for_call_id(call_id)
        if participant is not None:
            return participant
    # The goal arrived outside a scripted read (for example in the initial
    # message of a profile without swarm_board): key by its text.
    return f"px{zlib.crc32(message_text(message).encode('utf-8')):08x}"


def split_tool_spec(spec: str) -> tuple[str, str | None]:
    """``"swarm_board.read"`` -> ``("swarm_board", "read")``."""
    name, separator, action = spec.partition(".")
    return name, action if separator else None


def is_swarm_tool(spec: str) -> bool:
    return split_tool_spec(spec)[0] in SWARM_ACTIONS


def swarm_arguments(spec: str, *, tag: str, slot: int) -> dict[str, Any]:
    """Valid arguments for one Swarm Tool action; posts stay small filler text."""
    name, action = split_tool_spec(spec)
    actions = SWARM_ACTIONS.get(name)
    if actions is None:
        raise SwarmScriptError(f"{name!r} is not a Swarm Tool")
    chosen = action or actions[0]
    if chosen not in actions:
        known = ", ".join(actions)
        raise SwarmScriptError(f"no scripted {name} action {chosen!r} (known: {known})")
    if name == "swarm_board":
        if chosen == "post":
            return {"action": "post", "text": f"Progress note {tag} call {slot}: {_filler(tag)}"}
        return {"action": "read", "limit": BOARD_READ_LIMIT}
    if name == "swarm_wiki":
        if chosen == "create":
            return {
                "action": "create",
                "title": f"Notes {tag} call {slot}",
                "content": f"# Notes {tag}\n\n{_filler(tag)}\n",
            }
        return {"action": "list"}
    return {}


def _filler(tag: str) -> str:
    offset = zlib.crc32(tag.encode("utf-8"))
    return " ".join(_WORDS[(offset + index) % len(_WORDS)] for index in range(POST_WORDS))
